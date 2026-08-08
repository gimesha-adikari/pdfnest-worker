from __future__ import annotations

import pytest
from fastapi import HTTPException
from app.core.disk import check_disk_space


def test_check_disk_space_sufficient() -> None:
    # 1 KB requirement should pass
    check_disk_space(1024)


def test_check_disk_space_insufficient_raises_507() -> None:
    # 100 Petabytes requirement should raise HTTP 507
    excessive_bytes = 100 * 1024 * 1024 * 1024 * 1024 * 1024
    with pytest.raises(HTTPException) as exc_info:
        check_disk_space(excessive_bytes)

    assert exc_info.value.status_code == 507
    assert "Insufficient disk space" in exc_info.value.detail
