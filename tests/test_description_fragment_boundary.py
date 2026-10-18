"""Unbalanced source markup must never become an Inspector control ancestor."""
import pytest
from frontend.description_view import render_description


@pytest.mark.parametrize('raw', ['[b]Full English supported.[b]', '<b><i>Nested',
                               '<table><tr><td>Value', '<a href="https://example.com">Link'])
def test_description_is_a_closed_fragment(raw):
    from html.parser import HTMLParser
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__(); self.stack=[]
        def handle_starttag(self, tag, attrs):
            if tag!='br': self.stack.append(tag)
        def handle_endtag(self, tag):
            assert self.stack.pop()==tag
    tags=Tags(); tags.feed(render_description(raw)); tags.close()
    assert tags.stack==[]


def test_unmatched_closing_tags_cannot_close_the_host():
    assert render_description('</b></table>Text<script>bad()</script>')=='Text'
