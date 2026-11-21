"""Filter explicit webpage decorations, leaving ordinary prose and fenced code intact."""
import os
import posixpath
import re

_SECTION = re.compile(r'\[\[section:[^\]]*\]\]', re.IGNORECASE)
_PREFIXES = ('[[section:', '[[suggested_questions')
_SUGGESTIONS = re.compile(r'(?m)^[ \t]*\[\[(?:suggested_questions|section:suggested_questions)\]\]', re.IGNORECASE)
_FENCE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)\r?$')
_FENCED_SUGGESTIONS = re.compile(
    r'(?mi)^(?:[ \t]*\[\[(?!suggested_questions\b|section:suggested_questions\b)section:[^\]]*\]\])*[ \t]{0,3}(`{3,}|~{3,})[ \t]*suggested_questions(?=[ \t\r\n]|$)[^\r\n]*(?:\r?\n|$)'
)
_ADJOINED_CLOSE_SUGGESTION = re.compile(
    r'(?mi)(^[ \t]{0,3}`{3}[ \t]*)(?=(?:`{3,}|~{3,})[ \t]*suggested_questions\b|\[\[(?:suggested_questions|section:suggested_questions)\]\])'
)
_ADJOINED_CLOSE_SUGGESTION_AFTER_NL = re.compile(
    r'(?i)(\n[ \t]{0,3}`{3}[ \t]*)(?=(?:`{3,}|~{3,})[ \t]*suggested_questions\b|\[\[(?:suggested_questions|section:suggested_questions)\]\])'
)
_FENCE_PREFIX_HOLD_RE = re.compile(r'^[ \t]{0,3}(?:`+|~+)?$')
_FENCE_OPEN_PREFIX_RE = re.compile(r'^([ \t]{0,3})(`{3,}|~{3,})(.*)$', re.DOTALL)
_POSIX_ABS_ROOT_RE = re.compile(
    r'^/(?:Users|Volumes|Applications|private/(?:var|tmp)|var/folders|tmp|home|mnt|opt)/'
)
_WIN_DRIVE_ROOT_RE = re.compile(r'^[A-Za-z]:[\\/]')
_WIN_UNC_ROOT_RE = re.compile(r'^(?:\\\\|//)[^\\/\s]+[\\/][^\\/\s]+[\\/]')
_LINE_ANCHOR_RE = re.compile(r'(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)$')
_FORBIDDEN_PATH_CHARS_RE = re.compile(r'[\x00-\x1f"<>|*?;&$`\t]')
_EXT_FILENAME_RE = re.compile(
    r'^(?:\.[A-Za-z0-9][A-Za-z0-9._-]*|Makefile|Dockerfile|README|LICENSE|.+\.[A-Za-z0-9]{1,12})$'
)


def _format_local_file_markdown_link(raw: str) -> str | None:
    if not raw or raw != raw.strip() or len(raw) > 512 or raw.endswith(('/', '\\')):
        return None
    is_win_drive = bool(_WIN_DRIVE_ROOT_RE.match(raw))
    is_win_unc = bool(not is_win_drive and _WIN_UNC_ROOT_RE.match(raw))
    is_posix = bool(not is_win_drive and not is_win_unc and _POSIX_ABS_ROOT_RE.match(raw))
    if not (is_win_drive or is_win_unc or is_posix):
        return None
    if _FORBIDDEN_PATH_CHARS_RE.search(raw) or ' --' in raw:
        return None
    if is_posix and '\\' in raw:
        return None
    m_anchor = _LINE_ANCHOR_RE.search(raw)
    anchor = m_anchor.group(0) if m_anchor else ''
    base_path = raw[: m_anchor.start()] if m_anchor else raw
    if not base_path or base_path.endswith(('/', '\\')):
        return None
    if is_win_drive:
        if ':' in base_path[2:] or '\\\\' in base_path:
            return None
    elif is_win_unc:
        if ':' in base_path or '\\\\' in base_path[2:]:
            return None
    norm_base = base_path.replace('\\', '/')
    is_app_bundle = norm_base.lower().endswith('.app')
    exists = False
    # Never probe remote UNC shares (\\server\share) with os.path.exists on Windows:
    # SMB lookup on an unreachable host stalls the event loop for 30-45s.
    if (os.name == 'nt' and is_win_drive) or (os.name != 'nt' and is_posix):
        try:
            exists = os.path.exists(base_path)
        except OSError:
            exists = False
    if exists:
        if os.path.isdir(base_path) and not is_app_bundle:
            return None
        if not os.path.isfile(base_path) and not is_app_bundle:
            return None
    else:
        name_only = posixpath.basename(norm_base)
        if not name_only or not _EXT_FILENAME_RE.match(name_only):
            return None
    name = posixpath.basename(norm_base)
    label = f'{name}{anchor}'.replace('[', '\\[').replace(']', '\\]')
    target = f'{norm_base}{anchor}' if (is_win_drive or is_win_unc) else raw
    if any(ch in target for ch in (' ', '(', ')')):
        return f'[{label}](<{target}>)'
    return f'[{label}]({target})'


def _scan_line_backticks(line: str, allow_incomplete_tail: bool) -> tuple[str, str]:
    if not line:
        return '', ''
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        tick_pos = line.find('`', i)
        if tick_pos == -1:
            if allow_incomplete_tail and line.endswith('['):
                out.append(line[i:-1])
                return ''.join(out), '['
            out.append(line[i:])
            return ''.join(out), ''
        j = tick_pos
        while j < n and line[j] == '`':
            j += 1
        tick_len = j - tick_pos
        if j == n and allow_incomplete_tail:
            hold_start = tick_pos - 1 if (tick_pos > i and line[tick_pos - 1] == '[') else tick_pos
            out.append(line[i:hold_start])
            return ''.join(out), line[hold_start:]
        k = j
        matched = False
        while True:
            c_start = line.find('`', k)
            if c_start == -1:
                if allow_incomplete_tail:
                    hold_start = tick_pos - 1 if (tick_pos > i and line[tick_pos - 1] == '[') else tick_pos
                    out.append(line[i:hold_start])
                    return ''.join(out), line[hold_start:]
                out.append(line[i:j])
                i = j
                matched = True
                break
            c_end = c_start
            while c_end < n and line[c_end] == '`':
                c_end += 1
            if c_end == n and allow_incomplete_tail:
                hold_start = tick_pos - 1 if (tick_pos > i and line[tick_pos - 1] == '[') else tick_pos
                out.append(line[i:hold_start])
                return ''.join(out), line[hold_start:]
            if c_end - c_start == tick_len:
                if tick_len == 1 and not (tick_pos > 0 and line[tick_pos - 1] == '['):
                    out.append(line[i:tick_pos])
                    repl = _format_local_file_markdown_link(line[j:c_start])
                    out.append(repl if repl is not None else line[tick_pos:c_end])
                else:
                    out.append(line[i:c_end])
                i = c_end
                matched = True
                break
            k = c_end
        if not matched:
            out.append(line[i:])
            return ''.join(out), ''
    return ''.join(out), ''


class _LocalFileLinkStream:
    def __init__(self) -> None:
        self._fence = ''
        self._in_opening_fence_line = False
        self._fence_line = ''
        self._at_line_start = True
        self._buf = ''

    def _feed_no_nl(self, seg: str) -> str:
        if not seg:
            return ''
        if self._fence:
            if not self._in_opening_fence_line and len(self._fence_line) < 128:
                self._fence_line += seg[: 128 - len(self._fence_line)]
            return seg
        self._buf += seg
        if self._at_line_start:
            if len(self._buf) <= 32 and _FENCE_PREFIX_HOLD_RE.match(self._buf):
                return ''
            m_open = _FENCE_OPEN_PREFIX_RE.match(self._buf)
            if m_open:
                self._fence = m_open.group(2)
                self._in_opening_fence_line = True
                self._fence_line = ''
                out, self._buf = self._buf, ''
                return out
            self._at_line_start = False
        emitted, self._buf = _scan_line_backticks(self._buf, allow_incomplete_tail=True)
        return emitted

    def _feed_nl(self) -> str:
        if self._fence:
            if self._in_opening_fence_line:
                self._in_opening_fence_line = False
                self._fence_line = ''
            else:
                m_close = _FENCE.match(self._fence_line.rstrip('\r'))
                if (
                    m_close
                    and m_close[1][0] == self._fence[0]
                    and len(m_close[1]) >= len(self._fence)
                    and not m_close[2].strip()
                ):
                    self._fence = ''
                    self._at_line_start = True
                self._fence_line = ''
            return '\n'
        if self._at_line_start:
            m_open = _FENCE.match(self._buf.rstrip('\r'))
            if m_open:
                self._fence = m_open[1]
                self._in_opening_fence_line = False
                self._fence_line = ''
                out, self._buf = self._buf + '\n', ''
                return out
        emitted, _ = _scan_line_backticks(self._buf, allow_incomplete_tail=False)
        self._buf = ''
        self._at_line_start = True
        return emitted + '\n'

    def feed(self, text: str) -> str:
        if not text:
            return ''
        out: list[str] = []
        pos = 0
        while True:
            nl = text.find('\n', pos)
            if nl == -1:
                piece = self._feed_no_nl(text[pos:])
                if piece:
                    out.append(piece)
                break
            if nl > pos:
                piece = self._feed_no_nl(text[pos:nl])
                if piece:
                    out.append(piece)
            out.append(self._feed_nl())
            pos = nl + 1
        return ''.join(out)

    def finish(self) -> str:
        if not self._buf:
            return ''
        if self._fence:
            out, self._buf = self._buf, ''
            return out
        m_open = _FENCE.match(self._buf.rstrip('\r')) if self._at_line_start else None
        if m_open:
            self._fence = m_open[1]
            out, self._buf = self._buf, ''
            return out
        emitted, _ = _scan_line_backticks(self._buf, allow_incomplete_tail=False)
        self._buf = ''
        return emitted


def linkify_local_files(text: str) -> str:
    if not text or '`' not in text:
        return text
    stream = _LocalFileLinkStream()
    return stream.feed(text) + stream.finish()


def _fence_after(text, fence='', line=''):
    lines = (line + text).split('\n')
    for completed in lines[:-1]:
        cleaned = _SECTION.sub('', completed.rstrip('\r'))
        match = _FENCE.match(cleaned)
        if match:
            marker, info = match[1], match[2].strip()
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not info:
                fence = ''
    tail = lines[-1]
    return fence, tail[:256]


def _find_top_level_suggestion(text, fence='', line=''):
    best = None
    for match in _SUGGESTIONS.finditer(text):
        current_fence, prefix = _fence_after(text[:match.start()], fence, line)
        if not current_fence and not prefix.strip():
            best = (match.start(), match.end(), '')
            break
    for match in _FENCED_SUGGESTIONS.finditer(text):
        current_fence, prefix = _fence_after(text[:match.start()], fence, line)
        if not current_fence and not _SECTION.sub('', prefix).strip():
            cand = (match.start(), match.end(), match[1])
            if best is None or cand[0] < best[0]:
                best = cand
            break
    return best


def _is_potential_suggestion_line(line_tail: str) -> bool:
    line_tail = line_tail.rstrip('\r')
    if not line_tail or '\n' in line_tail or len(line_tail) > 128:
        return False
    stripped_sec = re.sub(
        r'^(?:[ \t]*\[\[(?!suggested_questions\b|section:suggested_questions\b)section:[^\]]*\]\])+',
        '',
        line_tail,
        flags=re.IGNORECASE,
    )
    if stripped_sec.strip(' \t') == '':
        return len(stripped_sec) <= 3
    s = stripped_sec.lstrip(' \t')
    leading_ws = len(stripped_sec) - len(s)
    if s.startswith('['):
        low = s.lower()
        return any(
            target.startswith(low)
            for target in ('[[suggested_questions]]', '[[section:suggested_questions]]')
        )
    if leading_ws <= 3 and s[0] in ('`', '~'):
        m = re.match(r'^(`+|~+)([ \t]*)(.*)$', s)
        if not m:
            return False
        ticks, spaces, rest = m[1], m[2], m[3].lower()
        if len(ticks) < 3:
            return spaces == '' and rest == ''
        if rest == '' or 'suggested_questions'.startswith(rest):
            return True
        if re.match(r'^suggested_questions(?![a-z0-9_-])', rest):
            return True
    return False


def _clean_web_decorations(text: str) -> str:
    if not text:
        return ''
    text = _ADJOINED_CLOSE_SUGGESTION.sub(r'\1\n', text)
    lines = text.splitlines(keepends=True)
    kept = []
    outer_fence = ''
    skip_suggestion_fence = ''
    for raw_line in lines:
        stripped_eol = raw_line.rstrip('\r\n')
        if skip_suggestion_fence:
            m = _FENCE.match(stripped_eol)
            if (
                m
                and m[1][0] == skip_suggestion_fence[0]
                and len(m[1]) >= len(skip_suggestion_fence)
                and not m[2].strip()
            ):
                skip_suggestion_fence = ''
            continue
        if not outer_fence and re.match(
            r'^[ \t]*\[\[(?:suggested_questions|section:suggested_questions)\]\]',
            stripped_eol,
            re.IGNORECASE,
        ):
            break
        cleaned_line = _SECTION.sub('', stripped_eol)
        m = _FENCE.match(cleaned_line)
        if not outer_fence:
            if m and re.match(r'^suggested_questions(?:\b|$)', m[2].strip(), re.IGNORECASE):
                skip_suggestion_fence = m[1]
                continue
            if m:
                outer_fence = m[1]
        elif (
            m
            and m[1][0] == outer_fence[0]
            and len(m[1]) >= len(outer_fence)
            and not m[2].strip()
        ):
            outer_fence = ''
        kept.append(raw_line)
    return _SECTION.sub('\n\n', ''.join(kept))


def clean_web_text(text: str) -> str:
    return linkify_local_files(_clean_web_decorations(text))


class WebTextFilter:
    def __init__(self):
        self.pending = ''
        self.suggestions = False
        self.skip_suggestion_fence = ''
        self.fence = ''
        self.line = ''
        self._link_stream = _LocalFileLinkStream()

    def _emit(self, text):
        if not text:
            return ''
        self.fence, self.line = _fence_after(text, self.fence, self.line)
        return self._link_stream.feed(text)

    def feed(self, text: str) -> str:
        if self.suggestions:
            return ''
        text = self.pending + text
        self.pending = ''
        if not self.fence or self.fence == '```':
            clean_prev_line = self.line.rstrip('\r')
            if (
                re.match(r'^[ \t]{0,3}`{3}[ \t]*$', clean_prev_line)
                and re.match(
                    r'^(?:(?:`{3,}|~{3,})[ \t]*suggested_questions(?=[ \t\r\n])|\[\[(?:suggested_questions|section:suggested_questions)\]\])',
                    text,
                    re.IGNORECASE,
                )
            ):
                text = '\n' + text
            elif (
                self.fence == '```'
                and re.match(r'^[ \t]{0,3}`{3}[ \t]*$', clean_prev_line)
                and '\n' not in text
                and _is_potential_suggestion_line(text)
            ):
                self.pending = text
                return ''
            if not self.line.strip():
                text = _ADJOINED_CLOSE_SUGGESTION.sub(r'\1\n', text)
            else:
                text = _ADJOINED_CLOSE_SUGGESTION_AFTER_NL.sub(r'\1\n', text)
        out_parts = []

        while text:
            if self.skip_suggestion_fence:
                close_re = re.compile(
                    r'(?m)^[ \t]{0,3}'
                    + re.escape(self.skip_suggestion_fence[0])
                    + '{'
                    + str(len(self.skip_suggestion_fence))
                    + r',}[ \t]*\r?\n'
                )
                close_match = close_re.search(text)
                if not close_match:
                    self.pending = text
                    return ''.join(out_parts)
                self.skip_suggestion_fence = ''
                self.fence = ''
                self.line = ''
                text = text[close_match.end():]
                continue

            hit = _find_top_level_suggestion(text, self.fence, self.line)
            if hit is not None:
                start, end, fence_marker = hit
                before = _SECTION.sub('\n\n', text[:start])
                if before:
                    out_parts.append(self._emit(before))
                if not fence_marker:
                    self.suggestions = True
                    return ''.join(out_parts)
                if not text[start:end].endswith('\n'):
                    self.pending = text[start:]
                    return ''.join(out_parts)
                self.skip_suggestion_fence = fence_marker
                self.fence = ''
                self.line = ''
                text = text[end:]
                continue
            break

        if not text:
            return ''.join(out_parts)

        # Hold back incomplete trailing top-level line that may become a suggestion block
        last_nl = text.rfind('\n')
        if last_nl >= 0:
            cand_fence, _ = _fence_after(text[: last_nl + 1], self.fence, self.line)
            tail_line = text[last_nl + 1 :]
            if not cand_fence and _is_potential_suggestion_line(tail_line):
                head = _SECTION.sub('\n\n', text[: last_nl + 1])
                self.pending = tail_line
                if head:
                    out_parts.append(self._emit(head))
                return ''.join(out_parts)
        elif not self.fence and _is_potential_suggestion_line(self.line + text):
            self.pending = text
            return ''.join(out_parts)

        text = _SECTION.sub('\n\n', text)
        lowered = text.lower()
        for prefix in _PREFIXES:
            start = lowered.rfind(prefix)
            if start >= 0 and ']]' not in text[start:] and len(text) - start < 512:
                self.pending = text[start:]
                out_parts.append(self._emit(text[:start]))
                return ''.join(out_parts)
        for length in range(min(max(map(len, _PREFIXES)) - 1, len(text)), 0, -1):
            if any(prefix.startswith(lowered[-length:]) for prefix in _PREFIXES):
                self.pending = text[-length:]
                out_parts.append(self._emit(text[:-length]))
                return ''.join(out_parts)
        out_parts.append(self._emit(text))
        return ''.join(out_parts)

    def finish(self) -> str:
        if self.suggestions or self.skip_suggestion_fence:
            self.pending = ''
            self.skip_suggestion_fence = ''
            return self._link_stream.finish()
        tail, self.pending = self.pending, ''
        if not tail:
            return self._link_stream.finish()
        if not self.fence and not self.line:
            tail = _clean_web_decorations(tail)
        return self._emit(tail) + self._link_stream.finish()
