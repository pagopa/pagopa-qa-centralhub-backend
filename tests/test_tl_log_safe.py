import uuid

from app.services.test_lifecycle.log_safe import log_safe


def test_log_safe_removes_line_breaks():
    assert log_safe("PQ-1\r\nFAKE entry\nother") == "PQ-1\\r\\nFAKE entry\\nother"


def test_log_safe_keeps_plain_values():
    fid = uuid.uuid4()
    assert log_safe(fid) == str(fid)
    assert log_safe(None) == "None"
