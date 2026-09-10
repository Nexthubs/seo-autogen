"""P2 URL normalization tests (spec section 14)."""

from app.services.url_normalizer import content_hash, normalize_url, url_hash


def test_lowercase_hostname():
    assert normalize_url("https://Example.COM/Path") == "https://example.com/Path"


def test_remove_fragment():
    assert (
        normalize_url("https://a.com/page#section-2") == "https://a.com/page"
    )


def test_remove_default_port():
    assert normalize_url("http://a.com:80/x") == "http://a.com/x"
    assert normalize_url("https://a.com:443/x") == "https://a.com/x"


def test_keep_non_default_port():
    assert normalize_url("https://a.com:8443/x") == "https://a.com:8443/x"


def test_remove_trailing_slash():
    assert normalize_url("https://a.com/path/") == "https://a.com/path"
    # root path keeps its single slash
    assert normalize_url("https://a.com/") == "https://a.com/"


def test_remove_tracking_params():
    assert (
        normalize_url(
            "https://a.com/p?utm_source=google&utm_medium=cpc"
            "&utm_campaign=x&utm_term=t&utm_content=c&gclid=abc"
            "&fbclid=xyz&ref=r&source=s"
        )
        == "https://a.com/p"
    )


def test_keep_and_sort_other_params():
    assert (
        normalize_url("https://a.com/p?b=2&a=1&utm_source=x")
        == "https://a.com/p?a=1&b=2"
    )
    # two URLs differing only in param order normalize identically
    assert (
        normalize_url("https://a.com/p?b=2&a=1")
        == normalize_url("https://a.com/p?a=1&b=2")
    )


def test_scheme_defaulted_to_https():
    assert normalize_url("a.com/p") == "https://a.com/p"


def test_case_insensitive_tracking_param_names():
    assert normalize_url("https://a.com/p?UTM_SOURCE=x") == "https://a.com/p"


def test_invalid_scheme_rejected():
    import pytest

    with pytest.raises(ValueError):
        normalize_url("ftp://a.com/p")


def test_url_hash_stable_for_equivalent_urls():
    a = url_hash("HTTPS://Example.COM/p?utm_source=x")
    b = url_hash("https://example.com/p")
    assert a == b
    assert len(a) == 64


def test_url_hash_differs_for_different_paths():
    assert url_hash("https://a.com/p") != url_hash("https://a.com/q")


def test_content_hash_whitespace_insensitive():
    assert (
        content_hash("Hello   World\n\nFoo")
        == content_hash("Hello World Foo")
    )


def test_content_hash_differs_for_different_content():
    assert content_hash("abc") != content_hash("abd")
