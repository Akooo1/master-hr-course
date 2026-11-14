#!/usr/bin/env python3
"""Build the interactive course app and the consolidated glossary from the Markdown sources.

Usage:  python3 tools/build.py

Two programmes ("tracks") share one app:
  academy — 00-start/tmk-profile.md, module-*/README.md, module-*/day-*.md
  english — english/README.md, english/module-*/README.md, english/module-*/lesson-*.md

Writes:  app/index.html          — standalone page (open in a browser, works offline)
         app/artifact.html       — same page without the document skeleton (for publishing as a hosted page)
         glossary.md             — RU–EN glossary merged from the terminology tables of all Academy days
         english/glossary.md     — EN–RU vocabulary of all English lessons
         english/phrasebook.md   — key phrases of all English lessons, by block

No third-party dependencies: the Markdown subset used in the course is rendered by the small converter below.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "tools" / "app_template.html"
LETTERS = "АБВГ"

# --------------------------------------------------------------------------------------
# Minimal Markdown renderer (headings, paragraphs, lists incl. nesting and task items,
# tables, blockquotes, fenced code, hr, <details> pass-through, inline bold/italic/code/links)
# --------------------------------------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![\*\w])")


def map_link(url: str, ctx: dict | None) -> tuple[str, bool]:
    """Map a link from the Markdown sources to the app. Returns (href, external)."""
    if re.match(r"^(https?:|mailto:)", url):
        return url, True
    if url.startswith("#"):
        return url, False
    track = (ctx or {}).get("track", "academy")
    if "tmk-profile.md" in url:
        return "#academy-ref", False
    day = re.search(r"day-(\d+)-", url)
    if day:
        return f"#academy-day-{int(day.group(1))}", False
    lesson = re.search(r"lesson-(\d+)-", url)
    if lesson:
        return f"#english-day-{int(lesson.group(1))}", False
    mod = re.search(r"(english/)?module-(\d+)-[^/]*/README\.md$", url)
    if mod:
        t = "english" if (mod.group(1) or (track == "english" and not url.startswith("../../module"))) else "academy"
        return f"#{t}-module-{int(mod.group(2))}", False
    if url.endswith("README.md") and ctx and ctx.get("module"):
        return f"#{track}-module-{ctx['module']}", False
    if "PROGRESS.md" in url:
        return f"#{track}-journal", False
    if "phrasebook.md" in url or "glossary.md" in url:
        return f"#{track}-cards", False
    return f"#{track}-today", False


def inline(text: str, ctx: dict | None = None) -> str:
    """Render inline Markdown to HTML. Code spans are protected from further processing."""
    codes: list[str] = []

    def keep_code(m: re.Match) -> str:
        codes.append("<code>" + html.escape(m.group(1), quote=False) + "</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(keep_code, text)
    text = html.escape(text, quote=False)

    def link(m: re.Match) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        href, external = map_link(url, ctx)
        attrs = ' target="_blank" rel="noopener"' if external else ""
        return f'<a href="{html.escape(href, quote=True)}"{attrs}>{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", line))


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


_LIST_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")


def _render_list(lines: list[str], ctx: dict | None) -> str:
    """Render a (possibly nested) list block."""
    items: list[dict] = []
    base_indent = None
    for line in lines:
        m = _LIST_RE.match(line)
        indent = len(line) - len(line.lstrip(" "))
        if m and (base_indent is None or indent <= base_indent):
            if base_indent is None:
                base_indent = indent
            items.append({"ordered": m.group(2)[0].isdigit(), "text": m.group(3), "children": []})
        elif items:
            if line.strip() and not m and indent <= (base_indent or 0):
                items[-1]["text"] += " " + line.strip()
            else:
                items[-1]["children"].append(line)
    if not items:
        return ""
    tag = "ol" if items[0]["ordered"] else "ul"
    out = [f"<{tag}>"]
    for it in items:
        text = it["text"]
        task = re.match(r"^\[( |x|X)\]\s+(.*)$", text)
        if task:
            checked = " checked" if task.group(1).lower() == "x" else ""
            body = f'<span class="task"><input type="checkbox" disabled{checked}> {inline(task.group(2), ctx)}</span>'
        else:
            body = inline(text, ctx)
        children = list(it["children"])
        while children and not children[-1].strip():
            children.pop()
        while children and not children[0].strip():
            children.pop(0)
        child_html = ""
        if children:
            dedent = min(len(c) - len(c.lstrip(" ")) for c in children if c.strip())
            child_html = render_md("\n".join(c[dedent:] if c.strip() else "" for c in children), ctx)
        out.append(f"<li>{body}{child_html}</li>")
    out.append(f"</{tag}>")
    return "".join(out)


def render_md(text: str, ctx: dict | None = None) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para), ctx) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_para()
            j = i + 1
            code: list[str] = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            out.append('<pre class="code"><code>' + html.escape("\n".join(code), quote=False) + "</code></pre>")
            i = j + 1
            continue

        if stripped == "":
            flush_para()
            i += 1
            continue

        if stripped.startswith("<details") or stripped.startswith("</details") or stripped.startswith("<summary"):
            flush_para()
            out.append(stripped)
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2), ctx)}</h{level}>")
            i += 1
            continue

        if re.match(r"^-{3,}$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            flush_para()
            header = _split_row(lines[i])
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            th = "".join(f"<th>{inline(c, ctx)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{inline(c, ctx)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')
            i = j
            continue

        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            j = i
            while j < len(lines) and lines[j].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            out.append("<blockquote>" + render_md("\n".join(quote), ctx) + "</blockquote>")
            i = j
            continue

        if _LIST_RE.match(line):
            flush_para()
            block: list[str] = []
            j = i
            while j < len(lines):
                cur = lines[j]
                if cur.strip() == "":
                    # A blank line ends the list unless the next line continues it (indented or a new item).
                    nxt = lines[j + 1] if j + 1 < len(lines) else ""
                    if nxt.startswith("  ") and nxt.strip():
                        block.append("")
                        j += 1
                        continue
                    break
                if _LIST_RE.match(cur) or cur.startswith("  ") or block:
                    if not _LIST_RE.match(cur) and not cur.startswith("  ") and block:
                        # Lazy continuation of the previous item.
                        block[-1] += " " + cur.strip()
                        j += 1
                        continue
                    block.append(cur)
                    j += 1
                    continue
                break
            out.append(_render_list(block, ctx))
            i = j
            continue

        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Course parsing
# --------------------------------------------------------------------------------------


def split_sections(md: str) -> list[tuple[str, str]]:
    """Split a document into (heading, body) pairs on level-2 headings. The first pair has heading ''."""
    parts: list[tuple[str, str]] = []
    current_head, buf = "", []
    in_code = False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("## "):
            parts.append((current_head, "\n".join(buf)))
            current_head, buf = line[3:].strip(), []
        else:
            buf.append(line)
    parts.append((current_head, "\n".join(buf)))
    return parts


def parse_table(body: str) -> list[list[str]]:
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            rows = []
            for row in lines[i + 2:]:
                if not row.strip().startswith("|"):
                    break
                rows.append(_split_row(row))
            return rows
    return []


def parse_quiz(body: str, ctx: dict) -> list[dict]:
    questions: list[dict] = []
    blocks = re.split(r"(?m)^\*\*В(\d+)\.\*\*\s*", body)
    # blocks: [preamble, num, rest, num, rest, ...]
    for k in range(1, len(blocks), 2):
        rest = blocks[k + 1]
        lines = rest.split("\n")
        q_lines, options, expl_lines, answer = [], [], [], None
        state = "q"
        for line in lines:
            s = line.strip()
            opt = re.match(r"^- ([АБВГ])\)\s+(.*)$", s)
            if state == "q" and opt:
                state = "opts"
            if state == "q":
                if s:
                    q_lines.append(s)
            elif state == "opts":
                if opt:
                    options.append(opt.group(2))
                elif s.startswith("<details"):
                    state = "ans"
            elif state == "ans":
                if s.startswith("</details"):
                    break
                a = re.match(r"^\*\*([АБВГ])\.\*\*\s*(.*)$", s)
                if a and answer is None:
                    answer = LETTERS.index(a.group(1))
                    if a.group(2):
                        expl_lines.append(a.group(2))
                elif s:
                    expl_lines.append(s)
        if answer is None or len(options) < 2:
            raise ValueError(f"Cannot parse quiz question В{blocks[k]}")
        questions.append({
            "q": inline(" ".join(q_lines), ctx),
            "options": [inline(o, ctx) for o in options],
            "answer": answer,
            "explain": inline(" ".join(expl_lines), ctx),
        })
    return questions


def minutes_of(text: str) -> int:
    m = re.search(r"\((\d+)\s*мин", text)
    return int(m.group(1)) if m else 0


TRACKS = [
    {
        "id": "academy",
        "title": "Mining & Metallurgy Executive Search Academy",
        "short": "Академия Executive Search",
        "subtitle": "30 дней · 6 модулей · отрасль, технологии, поиск и оценка кандидатов",
        "kind": "academy",
        "root": "",
        "unit_glob": "day-*.md",
        "labels": {"day": "День", "dayGen": "дня", "days": "дни", "unit": "Модуль", "unitGen": "модуля",
                   "test": "Тест дня", "check": "Teach-back", "ref": "TMK", "mark": "74 W"},
        "refTitle": "Профиль TMK",
        "ref": "00-start/tmk-profile.md",
    },
    {
        "id": "english",
        "title": "English for Critical Metals HR",
        "short": "Английский для HR",
        "subtitle": "20 уроков · 4 блока · русско-английский курс для рекрутера в металлургии",
        "kind": "english",
        "root": "english",
        "unit_glob": "lesson-*.md",
        "labels": {"day": "Урок", "dayGen": "урока", "days": "уроки", "unit": "Блок", "unitGen": "блока",
                   "test": "Тест урока", "check": "Speaking check", "ref": "Курс", "mark": "En"},
        "refTitle": "О курсе",
        "ref": "english/README.md",
    },
]


def _cap(s: str) -> str:
    m = re.search(r"[A-Za-zА-Яа-яЁё]", s)
    return s[:m.start()] + s[m.start()].upper() + s[m.start() + 1:] if m else s


def parse_day(path: Path, module: dict, track: dict) -> dict:
    """Parse one Academy day or one English lesson."""
    md = path.read_text(encoding="utf-8")
    ctx = {"module": module["n"], "track": track["id"]}
    english = track["kind"] == "english"
    first = md.split("\n", 1)[0]
    m = re.match(r"^# (?:День|Урок) (\d+)\.\s*(.*)$", first)
    if not m:
        raise ValueError(f"{path}: first line must be '# День N. Title' or '# Урок N. Title'")
    n, title = int(m.group(1)), m.group(2).strip()

    sections = split_sections(md)
    intro = sections[0][1]
    header_lines = []
    for line in intro.split("\n")[1:]:
        if line.startswith(">"):
            header_lines.append(re.sub(r"^>\s?", "", line))
        elif line.strip() == "---":
            break
    header = "\n".join(header_lines)
    challenge = re.search(r"\*\*(?:Вызов дня|Задача урока)[^*]*:\*\*\s*(.+?)(?:\n|$)", header)
    why = re.search(r"\*\*Зачем это вам:\*\*\s*(.+?)(?:\n|$)", header)
    meta_line = header.split("\n")[0] if header else ""
    duration = re.search(r"≈\s*([\d,–\-]+\s*(?:час|мин)\w*)", meta_line)
    level = re.search(r"Уровень:\s*(.+)$", meta_line)

    checklist, min_version, terms, phrases, quiz = [], "", [], [], []
    content_parts = []
    for head, body in sections[1:]:
        if head.startswith("Чек-лист"):
            for line in body.split("\n"):
                t = re.match(r"^- \[ \]\s+(.*)$", line.strip())
                if t:
                    checklist.append({"text": inline(t.group(1), ctx), "minutes": minutes_of(t.group(1))})
                mv = re.match(r"^>\s*\*\*Минимальная версия[^*]*\*\*\s*(.*)$", line.strip())
                if mv:
                    min_version = inline(mv.group(1), ctx)
            continue
        if re.match(r"^\d+\. Тест (дня|урока)", head):
            quiz = parse_quiz(body, ctx)
            continue
        if not english and head.startswith("2. Терминология"):
            for row in parse_table(body):
                if len(row) >= 3:
                    terms.append({"ru": row[0], "en": row[1], "def": row[2]})
        if english and head.startswith("1. Словарь"):
            for row in parse_table(body):
                if len(row) >= 4:
                    terms.append({"en": row[0], "pron": row[1], "ru": row[2], "ex": row[3]})
        if english and head.startswith("2. Ключевые фразы"):
            for row in parse_table(body):
                if len(row) >= 3:
                    phrases.append({"en": row[0], "ru": row[1], "note": row[2]})
        content_parts.append(f"## {head}\n{body}")

    if len(quiz) != 10:
        raise ValueError(f"{path}: expected 10 quiz questions, found {len(quiz)}")
    if not checklist:
        raise ValueError(f"{path}: checklist not found")
    if english:
        if len(terms) < 15:
            raise ValueError(f"{path}: expected at least 15 vocabulary rows, found {len(terms)}")
        if len(phrases) < 8:
            raise ValueError(f"{path}: expected at least 8 key phrases, found {len(phrases)}")
    elif len(terms) < 20:
        raise ValueError(f"{path}: expected at least 20 terms, found {len(terms)}")

    return {
        "n": n,
        "module": module["n"],
        "title": title,
        "file": f"{module['dir']}/{path.name}",
        "duration": duration.group(1) if duration else "",
        "level": level.group(1).strip() if level else "",
        "challenge": inline(_cap(" ".join(challenge.group(1).split())), ctx) if challenge else "",
        "why": inline(_cap(" ".join(why.group(1).split())), ctx) if why else "",
        "checklist": checklist,
        "minVersion": min_version,
        "terms": terms,
        "phrases": phrases,
        "quiz": quiz,
        "html": render_md("\n".join(content_parts), ctx),
    }


def parse_module(dir_path: Path, track: dict) -> dict:
    readme = (dir_path / "README.md").read_text(encoding="utf-8")
    first = readme.split("\n", 1)[0]
    m = re.match(r"^# (?:Модуль|Блок) (\d+)\.\s*(.*?)\s*\((?:Дни|Уроки) (\d+)[–-](\d+)\)", first)
    if not m:
        raise ValueError(f"{dir_path}/README.md: unexpected title line")
    module = {
        "n": int(m.group(1)),
        "title": m.group(2),
        "from": int(m.group(3)),
        "to": int(m.group(4)),
        "dir": (f"{track['root']}/" if track["root"] else "") + dir_path.name,
        "ready": True,
    }
    ctx = {"module": module["n"], "track": track["id"]}
    goal = re.search(r"^>\s*\*\*Практический результат (?:модуля|блока):\*\*\s*(.+)$", readme, re.M)
    module["goal"] = inline(goal.group(1), ctx) if goal else ""
    test_html, teach_html = "", ""
    for head, body in split_sections(readme):
        if head.startswith("Итоговый тест"):
            test_html = render_md(body, ctx)
        if head.startswith("Teach-back") or head.startswith("Speaking check"):
            teach_html = render_md(body, ctx)
    if not test_html or not teach_html:
        raise ValueError(f"{dir_path}/README.md: final test or teach-back / speaking check section missing")
    module["testHtml"] = test_html
    module["teachHtml"] = teach_html
    return module


def build_glossary(days: list[dict]) -> str:
    merged: dict[str, dict] = {}
    for d in days:
        for t in d["terms"]:
            key = t["ru"].lower()
            if key not in merged:
                merged[key] = {"ru": t["ru"], "en": t["en"], "def": t["def"], "days": [d["n"]]}
            elif d["n"] not in merged[key]["days"]:
                merged[key]["days"].append(d["n"])
    entries = sorted(merged.values(), key=lambda e: e["ru"].lower().replace("ё", "е"))
    lines = [
        "# Глоссарий RU — EN",
        "",
        "> Сводный словарь терминов программы, собранный из терминологических таблиц всех дней.",
        "> Файл генерируется командой `python3 tools/build.py` — правьте термины в файлах дней, а не здесь.",
        "",
        f"Терминов: **{len(entries)}**.",
        "",
        "| Русский | English | Что это значит | День |",
        "|---|---|---|---|",
    ]
    for e in entries:
        refs = ", ".join(str(n) for n in sorted(e["days"]))
        lines.append(f"| {e['ru']} | {e['en']} | {e['def']} | {refs} |")
    return "\n".join(lines) + "\n"


def build_english_glossary(days: list[dict]) -> str:
    merged: dict[str, dict] = {}
    for d in days:
        for t in d["terms"]:
            key = t["en"].lower()
            if key not in merged:
                merged[key] = {**t, "days": [d["n"]]}
            elif d["n"] not in merged[key]["days"]:
                merged[key]["days"].append(d["n"])
    entries = sorted(merged.values(), key=lambda e: e["en"].lower())
    lines = [
        "# Glossary EN — RU",
        "",
        "> Словарь курса English for Critical Metals HR, собранный из разделов «Словарь» всех уроков.",
        "> Файл генерируется командой `python3 tools/build.py` — правьте слова в файлах уроков, а не здесь.",
        "",
        f"Слов и выражений: **{len(entries)}**.",
        "",
        "| English | Произношение | Русский | Урок |",
        "|---|---|---|---|",
    ]
    for e in entries:
        refs = ", ".join(str(n) for n in sorted(e["days"]))
        lines.append(f"| {e['en']} | {e['pron']} | {e['ru']} | {refs} |")
    return "\n".join(lines) + "\n"


def build_phrasebook(days: list[dict], modules: list[dict]) -> str:
    lines = [
        "# Phrasebook: фразы рекрутера",
        "",
        "> Все ключевые фразы курса English for Critical Metals HR по урокам. Держите файл открытым во время звонков и переписки.",
        "> Файл генерируется командой `python3 tools/build.py` — правьте фразы в файлах уроков, а не здесь.",
        "",
    ]
    for m in modules:
        lines += [f"## Блок {m['n']}. {m['title']}", ""]
        for d in days:
            if d["module"] != m["n"]:
                continue
            lines += [f"### Урок {d['n']}. {d['title']}", "", "| English | Русский | Когда использовать |", "|---|---|---|"]
            lines += [f"| {p['en']} | {p['ru']} | {p['note']} |" for p in d["phrases"]]
            lines.append("")
    return "\n".join(lines)


def build_track(track: dict) -> dict:
    base = ROOT / track["root"] if track["root"] else ROOT
    modules, days = [], []
    for mdir in sorted(base.glob("module-*")):
        if not (mdir / "README.md").exists():
            continue
        module = parse_module(mdir, track)
        modules.append(module)
        for dpath in sorted(mdir.glob(track["unit_glob"])):
            days.append(parse_day(dpath, module, track))
    days.sort(key=lambda d: d["n"])
    modules.sort(key=lambda m: m["n"])
    if [d["n"] for d in days] != list(range(1, len(days) + 1)):
        raise ValueError(f"{track['id']}: unit numbers are not contiguous: {[d['n'] for d in days]}")
    for m in modules:
        inside = [d["n"] for d in days if d["module"] == m["n"]]
        if inside != list(range(m["from"], m["to"] + 1)):
            raise ValueError(f"{track['id']} module {m['n']}: expected units {m['from']}–{m['to']}, found {inside}")
    ref = (ROOT / track["ref"]).read_text(encoding="utf-8")
    if track["kind"] == "english":
        # The course README starts with its own title; the app shows it under the "О курсе" tab.
        ref = ref.split("\n", 1)[1]
    return {
        "id": track["id"],
        "title": track["title"],
        "short": track["short"],
        "subtitle": track["subtitle"],
        "kind": track["kind"],
        "totalDays": len(days),
        "labels": track["labels"],
        "modules": modules,
        "days": days,
        "refTitle": track["refTitle"],
        "refHtml": render_md(ref, {"track": track["id"]}),
    }


def main() -> int:
    tracks = [build_track(t) for t in TRACKS]
    academy = next(t for t in tracks if t["id"] == "academy")
    english = next(t for t in tracks if t["id"] == "english")

    (ROOT / "glossary.md").write_text(build_glossary(academy["days"]), encoding="utf-8")
    (ROOT / "english" / "glossary.md").write_text(build_english_glossary(english["days"]), encoding="utf-8")
    (ROOT / "english" / "phrasebook.md").write_text(build_phrasebook(english["days"], english["modules"]),
                                                    encoding="utf-8")

    data = {"version": 2, "tracks": tracks}
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if "/*__COURSE_DATA__*/null" not in template:
        raise ValueError("Template placeholder /*__COURSE_DATA__*/null not found")
    page = template.replace("/*__COURSE_DATA__*/null", payload)

    app_dir = ROOT / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "artifact.html").write_text(page, encoding="utf-8")
    standalone = (
        '<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        "</head>\n<body>\n" + page + "\n</body>\n</html>\n"
    )
    (app_dir / "index.html").write_text(standalone, encoding="utf-8")

    for t in tracks:
        n_terms = sum(len(d["terms"]) for d in t["days"])
        n_phr = sum(len(d.get("phrases", [])) for d in t["days"])
        print(f"{t['id']}: {len(t['modules'])} modules, {len(t['days'])} units, "
              f"{sum(len(d['quiz']) for d in t['days'])} quiz questions, {n_terms} terms, {n_phr} phrases")
    print(f"-> app/index.html ({len(standalone.encode('utf-8')) // 1024} KB), app/artifact.html, glossary.md, "
          f"english/glossary.md, english/phrasebook.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
