import pytest

from orca_proxy.validation import _validate_injection
from orca_proxy.errors import ValidationFailed


def test_custom_header_configuration():
    value = {'type': 'header', 'name': 'X-API-Key'}
    assert _validate_injection(value) == value


@pytest.mark.parametrize('name', ['', 'bad name', 'X-Key\r\nInjected: yes', ':authority', 'café', None])
def test_invalid_custom_header_names(name):
    with pytest.raises(ValidationFailed):
        _validate_injection({'type': 'header', 'name': name})
