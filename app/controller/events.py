"""Bounded, strictly ordered callback envelope shared by both trust boundaries."""
import json
from app.core.native_domain import DomainUnavailable
from app.controller.profiles import CALLBACKS, MAX_EVENTS

def validate_event(event, expected):
    if (type(event) is not dict or set(event) != {'sequence','name','args'}
            or type(event['sequence']) is not int or event['sequence'] != expected
            or not 1 <= expected <= MAX_EVENTS or event['name'] not in CALLBACKS - {'cancellation_check'}):
        raise DomainUnavailable()
    if len(json.dumps(event,allow_nan=False).encode()) > 1024 * 1024:
        raise DomainUnavailable()
    # The parent's existing fixed codec validates record/enum classes and calls
    # only callbacks supplied by the local caller. Never import types here.
    if type(event['args']) is not dict or event['args'].get('$') != 'tuple':
        raise DomainUnavailable()
