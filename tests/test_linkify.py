"""Bare web addresses become links in what you read and what you send."""
from __future__ import annotations

from waddlemail.linkify import linkify_html
from waddlemail.ui.render import render_body


def link(url: str) -> str:
    return f'<a href="{url}" rel="noopener noreferrer">{url}</a>'


def test_bare_addresses_in_html_become_links():
    out = linkify_html("<p>See https://example.com/a?b=1&amp;c=2 now</p>")
    assert out == f"<p>See {link('https://example.com/a?b=1&amp;c=2')} now</p>"
    assert link("http://x.org") in linkify_html("<div><b>http://x.org</b></div>")


def test_punctuation_and_brackets_stay_outside():
    assert linkify_html("Go to http://x.org/a.") == f"Go to {link('http://x.org/a')}."
    assert linkify_html("(http://x.org/a)") == f"({link('http://x.org/a')})"
    assert linkify_html("http://x.org/a_(b)") == link("http://x.org/a_(b)")
    assert linkify_html("&quot;http://x.org&quot;") == f"&quot;{link('http://x.org')}&quot;"
    assert linkify_html("see https:// here") == "see https:// here"


def test_existing_links_tags_and_styles_are_left_alone():
    html = ('<a href="http://x.org">http://x.org</a><img src="http://x.org/i.png" alt="http://y.org">'
            '<style>a{background:url(http://x.org/b.png)}</style><span title="a > http://z.org">t</span>')
    assert linkify_html(html) == html
    nested = '<a href="http://x.org"><span>http://x.org</span></a> http://y.org'
    assert linkify_html(nested) == nested.replace(" http://y.org", f" {link('http://y.org')}")


def test_reader_links_plain_and_html_bodies():
    assert link("https://a.org/x") in render_body("text", "hi <https://a.org/x>\nbye")
    assert link("https://a.org/x") in render_body("html", "<p>hi https://a.org/x</p>")
    # still sanitised: nothing active gets through just because it looks like an address
    out = render_body("html", "<p onclick='x()'>https://a.org/x</p><script>http://e.com</script>")
    assert "onclick" not in out and "<script" not in out
