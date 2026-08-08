from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

LEASE_KEY_PREFIX = "pdfnest:limiter:lease:"
ACTIVE_GLOBAL_KEY = "pdfnest:limiter:active"
ACTIVE_IDENTITY_PREFIX = "pdfnest:limiter:identity:"

ATOMIC_ACQUIRE_LUA = """
local leaseKey = KEYS[1]
local activeKey = KEYS[2]
local identityKey = KEYS[3]

local taskId = ARGV[1]
local identityId = ARGV[2]
local globalCap = tonumber(ARGV[3])
local identityCap = tonumber(ARGV[4])
local ttlSeconds = tonumber(ARGV[5]) or 600
local now = tonumber(ARGV[6])
local expiresAt = now + ttlSeconds

local existingLease = redis.call('GET', leaseKey)
if existingLease and existingLease ~= '' then
    local data = cjson.decode(existingLease)
    if data and data.taskId == taskId then
        redis.call('EXPIRE', leaseKey, ttlSeconds)
        redis.call('ZADD', activeKey, expiresAt, taskId)
        redis.call('ZADD', identityKey, expiresAt, taskId)
        return cjson.encode({ status = "ACCEPTED", reason = "ALREADY_ACQUIRED", expiresAt = expiresAt })
    end
end

redis.call('ZREMRANGEBYSCORE', activeKey, '-inf', '(' .. now)
redis.call('ZREMRANGEBYSCORE', identityKey, '-inf', '(' .. now)

local globalCount = redis.call('ZCARD', activeKey)
if globalCount >= globalCap then
    return cjson.encode({ status = "REJECTED", reason = "GLOBAL_CAPACITY_EXHAUSTED", active = globalCount, max = globalCap })
end

local identityCount = redis.call('ZCARD', identityKey)
if identityCount >= identityCap then
    return cjson.encode({ status = "REJECTED", reason = "IDENTITY_CAPACITY_EXHAUSTED", active = identityCount, max = identityCap })
end

local leaseData = cjson.encode({
    taskId = taskId,
    identityId = identityId,
    acquiredAt = now,
    expiresAt = expiresAt
})

redis.call('SET', leaseKey, leaseData, 'EX', ttlSeconds)
redis.call('ZADD', activeKey, expiresAt, taskId)
redis.call('ZADD', identityKey, expiresAt, taskId)

return cjson.encode({ status = "ACCEPTED", reason = "ACQUIRED", expiresAt = expiresAt })
"""

ATOMIC_RELEASE_LUA = """
local leaseKey = KEYS[1]
local activeKey = KEYS[2]
local identityKey = KEYS[3]

local taskId = ARGV[1]
local identityId = ARGV[2]

local existingLease = redis.call('GET', leaseKey)
if existingLease and existingLease ~= '' then
    local data = cjson.decode(existingLease)
    if data and data.taskId == taskId then
        redis.call('DEL', leaseKey)
        redis.call('ZREM', activeKey, taskId)
        redis.call('ZREM', identityKey, taskId)
        return cjson.encode({ status = "RELEASED" })
    else
        return cjson.encode({ status = "MISMATCH" })
    end
end

redis.call('ZREM', activeKey, taskId)
redis.call('ZREM', identityKey, taskId)
return cjson.encode({ status = "NO_OP" })
"""

ATOMIC_RENEW_LUA = """
local leaseKey = KEYS[1]
local activeKey = KEYS[2]
local identityKey = KEYS[3]

local taskId = ARGV[1]
local identityId = ARGV[2]
local ttlSeconds = tonumber(ARGV[3]) or 600
local now = tonumber(ARGV[4])
local expiresAt = now + ttlSeconds

local existingLease = redis.call('GET', leaseKey)
if not existingLease or existingLease == '' then
    return cjson.encode({ status = "EXPIRED" })
end

local data = cjson.decode(existingLease)
if not data or data.taskId ~= taskId then
    return cjson.encode({ status = "MISMATCH" })
end

data.expiresAt = expiresAt
data.renewedAt = now
local newData = cjson.encode(data)

redis.call('SET', leaseKey, newData, 'EX', ttlSeconds)
redis.call('ZADD', activeKey, expiresAt, taskId)
redis.call('ZADD', identityKey, expiresAt, taskId)

return cjson.encode({ status = "RENEWED", expiresAt = expiresAt })
"""

_acquire_script = redis_client.register_script(ATOMIC_ACQUIRE_LUA)
_release_script = redis_client.register_script(ATOMIC_RELEASE_LUA)
_renew_script = redis_client.register_script(ATOMIC_RENEW_LUA)


def get_global_limit() -> int:
    return getattr(settings, "global_heavy_execution_limit", 4)


def get_identity_limit() -> int:
    return getattr(settings, "per_identity_heavy_execution_limit", 2)


def get_lease_ttl() -> int:
    return getattr(settings, "heavy_lease_ttl_seconds", 600)


def acquire_lease(task_id: str, identity_id: str | None = None) -> tuple[bool, str]:
    task_id = (task_id or "").strip()
    identity_id = (identity_id or "").strip() or "guest:anonymous"

    if not task_id:
        return False, "EMPTY_TASK_ID"

    lease_key = f"{LEASE_KEY_PREFIX}{task_id}"
    active_key = ACTIVE_GLOBAL_KEY
    identity_key = f"{ACTIVE_IDENTITY_PREFIX}{identity_id}"
    now = int(time.time())

    try:
        raw_res = _acquire_script(
            keys=[lease_key, active_key, identity_key],
            args=[
                task_id,
                identity_id,
                get_global_limit(),
                get_identity_limit(),
                get_lease_ttl(),
                now,
            ],
        )
        data: dict[str, Any] = json.loads(raw_res)
        status = data.get("status")
        reason = data.get("reason", "")

        if status == "ACCEPTED":
            logger.info("Acquired execution lease for task %s (identity: %s, reason: %s)", task_id, identity_id, reason)
            return True, reason

        logger.info("Lease acquisition rejected for task %s (identity: %s, reason: %s)", task_id, identity_id, reason)
        return False, reason
    except Exception as exc:
        logger.error("Redis error acquiring lease for task %s: %s", task_id, exc)
        return False, "REDIS_ERROR"


def release_lease(task_id: str, identity_id: str | None = None) -> None:
    task_id = (task_id or "").strip()
    identity_id = (identity_id or "").strip() or "guest:anonymous"

    if not task_id:
        return

    lease_key = f"{LEASE_KEY_PREFIX}{task_id}"
    active_key = ACTIVE_GLOBAL_KEY
    identity_key = f"{ACTIVE_IDENTITY_PREFIX}{identity_id}"

    try:
        _release_script(
            keys=[lease_key, active_key, identity_key],
            args=[task_id, identity_id],
        )
        logger.info("Released execution lease for task %s", task_id)
    except Exception as exc:
        logger.error("Redis error releasing lease for task %s: %s", task_id, exc)


def renew_lease(task_id: str, identity_id: str | None = None) -> bool:
    task_id = (task_id or "").strip()
    identity_id = (identity_id or "").strip() or "guest:anonymous"

    if not task_id:
        return False

    lease_key = f"{LEASE_KEY_PREFIX}{task_id}"
    active_key = ACTIVE_GLOBAL_KEY
    identity_key = f"{ACTIVE_IDENTITY_PREFIX}{identity_id}"
    now = int(time.time())

    try:
        raw_res = _renew_script(
            keys=[lease_key, active_key, identity_key],
            args=[task_id, identity_id, get_lease_ttl(), now],
        )
        data: dict[str, Any] = json.loads(raw_res)
        return data.get("status") == "RENEWED"
    except Exception as exc:
        logger.error("Redis error renewing lease for task %s: %s", task_id, exc)
        return False
