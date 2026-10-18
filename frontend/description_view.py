"""Small offline-only rich text surface; never resolves embedded resources."""
from html import escape
from html.parser import HTMLParser
import re
from PySide6.QtWidgets import QTextBrowser


class _Sanitizer(HTMLParser):
    allowed = {'p', 'br', 'b', 'strong', 'i', 'em', 'u', 's', 'ul', 'ol', 'li',
               'blockquote', 'pre', 'code', 'h1', 'h2', 'h3', 'table', 'tr', 'td', 'th'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object'):
            self.blocked += 1
        elif not self.blocked:
            if tag in self.allowed:
                self.parts.append('<' + tag + '>')
                if tag != 'br':
                    self.stack.append(tag)
            elif tag == 'img':
                self.parts.append('[图片未加载]')
            elif tag == 'a':
                href = dict(attrs).get('href', '')
                self.parts.append('<a href="' + escape(href, quote=True) + '">' if href.startswith(('https://', 'http://')) else '<a>')
                self.stack.append('a')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe', 'object'):
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in self.stack:
            while self.stack:
                opened = self.stack.pop()
                self.parts.append('</' + opened + '>')
                if opened == tag:
                    break

    def close(self):
        super().close()
        while self.stack:
            self.parts.append('</' + self.stack.pop() + '>')

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data).replace('\n', '<br>'))


def render_description(raw):
    text = str(raw or '')
    text = re.sub(r'\[img\].*?\[/img\]', '[图片未加载]', text, flags=re.I | re.S)
    mapping = {'b': 'b', 'i': 'i', 'u': 'u', 'strike': 's', 'h1': 'h1', 'h2': 'h2',
               'h3': 'h3', 'list': 'ul', 'olist': 'ol', 'quote': 'blockquote', 'code': 'pre'}
    text = re.sub(r'\[(/?)(b|i|u|strike|h1|h2|h3|list|olist|quote|code)\]',
                  lambda m: '<' + m[1] + mapping[m[2].lower()] + '>', text, flags=re.I)
    text = text.replace('[*]', '<li>')
    text = re.sub(r'\[url=([^\]]+)\]', lambda m: '<a href="' + escape(m[1], quote=True) + '">', text, flags=re.I)
    text = re.sub(r'\[/url\]', '</a>', text, flags=re.I)
    parser = _Sanitizer()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts)


class SafeDescriptionBrowser(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.document().setDefaultStyleSheet('p {margin: 4px 0;} li {margin: 3px 0;}')

    def loadResource(self, resource_type, url):
        return None

    def set_description(self, text):
        self.setHtml(render_description(text))
