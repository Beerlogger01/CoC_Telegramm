import pytest

from app.coc_client import InvalidTagError, encode_tag, normalize_tag


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("#2PRGP0L22", "#2PRGP0L22"),
        ("2prgp0l22", "#2PRGP0L22"),
        (" 2PRGP0L22 ", "#2PRGP0L22"),
    ],
)
def test_normalize_tag_valid(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


def test_normalize_tag_invalid() -> None:
    with pytest.raises(InvalidTagError):
        normalize_tag("#INVALID!")


def test_encode_tag() -> None:
    assert encode_tag("#2PRGP0L22") == "%232PRGP0L22"
