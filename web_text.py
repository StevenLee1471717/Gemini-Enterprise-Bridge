"""Filter explicit webpage decorations, leaving ordinary prose and fenced code intact."""
import re

_SECTION = re.compile(r'\[\[section:[^\]]*\]\]', re.IGNORECASE)
_PREFIXES = ('[[section:', '[[suggested_questions')
_SUGGESTIONS = re.compile(r'(?m)^[ \t]*\[\[(?:suggested_questions|section:suggested_questions)\]\]', re.IGNORECASE)
_FENCE = re.compile(r'^\s*(`{3,}|~{3,})')


def _fence_after(text, fence='', line=''):
    lines = (line + text).split('\n')
    for completed in lines[:-1]:
        match = _FENCE.match(completed)
        if match:
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ''
    tail = lines[-1]
    return fence, (' ' if tail and not tail.strip() else tail[:256])


def _suggestion_start(text, fence='', line=''):
    for match in _SUGGESTIONS.finditer(text):
        current_fence, prefix = _fence_after(text[:match.start()], fence, line)
        if not current_fence and not prefix.strip():
            return match.start()
    return None


def clean_web_text(text: str) -> str:
    start = _suggestion_start(text)
    if start is not None:
        text = text[:start]
    return _SECTION.sub('\n\n', text)


class WebTextFilter:
    def __init__(self):
        self.pending = ''
        self.suggestions = False
        self.fence = ''
        self.line = ''

    def _emit(self, text):
        self.fence, self.line = _fence_after(text, self.fence, self.line)
        return text

    def feed(self, text: str) -> str:
        if self.suggestions:
            return ''
        text = self.pending + text
        self.pending = ''
        start = _suggestion_start(text, self.fence, self.line)
        if start is not None:
            self.suggestions = True
            text = text[:start]
        text = _SECTION.sub('\n\n', text)
        lowered = text.lower()
        for prefix in _PREFIXES:
            start = lowered.rfind(prefix)
            if start >= 0 and ']]' not in text[start:] and len(text) - start < 512:
                self.pending = text[start:]
                return self._emit(text[:start])
        for length in range(min(max(map(len, _PREFIXES)) - 1, len(text)), 0, -1):
            if any(prefix.startswith(lowered[-length:]) for prefix in _PREFIXES):
                self.pending = text[-length:]
                return self._emit(text[:-length])
        return self._emit(text)

    def finish(self) -> str:
        tail, self.pending = self.pending, ''
        return self._emit(tail)
