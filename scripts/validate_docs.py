"""Validation of practice artifacts: team templates and team repositories.

The same module runs in three places: the control panel (checking its own
templates), a team repository CI pipeline, and developer machines. Therefore it
must stay dependency-light: PyYAML only, no imports from the control panel.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

Profile = Literal["team", "template", "coordination"]
Scope = Literal["full", "meta", "none"]

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fill_marker", re.compile(r"ЗАПОЛНИТЕ")),
    ("mustache", re.compile(r"\{\{[^}\n]{1,80}\}\}")),
    ("todo_marker", re.compile(r"<!--\s*TODO", re.IGNORECASE)),
    ("angle_placeholder", re.compile(r"<(ТОКЕН|ПЛЕЙСХОЛДЕР)>", re.IGNORECASE)),
)

DOCUMENT_SPECS: dict[str, dict[str, Any]] = {
    "docs/01_problem.md": {
        "frontmatter": ["doc", "stage", "team", "updated", "authors"],
        "sections": [
            "Контекст",
            "Противоречие",
            "Носитель проблемы",
            "Заинтересованные стороны",
            "Бизнес-цель",
            "Метрики успеха",
            "Границы решения",
            "Допущения",
        ],
        "mermaid": False,
    },
    "docs/02_asis_analysis.md": {
        "frontmatter": ["doc", "stage", "team", "updated", "authors"],
        "sections": [
            "Границы процесса",
            "Диаграмма процесса",
            "Шаги процесса",
            "Источники данных",
            "Обнаруженные проблемы",
            "Оценка потерь",
        ],
        "mermaid": True,
    },
    "docs/03_tobe_architecture.md": {
        "frontmatter": ["doc", "stage", "team", "updated", "authors"],
        "sections": [
            "Целевой процесс",
            "Обоснование альтернатив",
            "Архитектура решения",
            "Границы контура данных",
            "Human-in-the-Loop",
            "Риски ИИ и контрмеры",
            "Логирование и аудит",
        ],
        "mermaid": True,
    },
    "docs/04_requirements.md": {
        "frontmatter": ["doc", "stage", "team", "updated", "authors"],
        "sections": [
            "User Stories",
            "Use Cases",
            "Нефункциональные требования",
            "Ограничения и допущения",
            "Definition of Ready",
            "Definition of Done",
            "Трассируемость",
        ],
        "mermaid": False,
    },
    "docs/05_glossary.md": {
        "frontmatter": ["doc", "stage", "team", "updated", "authors"],
        "sections": [],
        "mermaid": False,
    },
}

OPTIONAL_TEAM_FILES = ("docs/05_glossary.md",)
REQUIRED_TEAM_FILES = tuple(path for path in DOCUMENT_SPECS if path not in OPTIONAL_TEAM_FILES)
REQUIRED_TEAM_PATHS = (*REQUIRED_TEAM_FILES, "data/meta.yml", "README.md")

# What the `team` profile reads, and therefore what a commit has to touch to be
# able to fail it. A change outside these paths cannot turn a full validation
# red, so running one on it only repeats an old verdict about documents nobody
# touched — right after provisioning that is the whole difference between a
# green check and an error about `{{ ... }}` that the team never left.
FULL_SCOPE_PREFIXES = ("docs/",)
# `data/meta.yml` is deliberately absent: it is metadata, and a change to it is
# what the `meta` scope exists for. Listing it here would send every roster
# change through the full validation and freeze a fresh team on its own
# scaffolding all over again.
FULL_SCOPE_FILES = frozenset({*REQUIRED_TEAM_FILES, "README.md", "scripts/validate_docs.py"})
META_SCOPE_PREFIXES = ("data/",)

REQUIRED_COORDINATION_FILES = (
    "README.md",
    "docs/reglament.md",
    "docs/timeline.md",
    "docs/faq.md",
    ".github/ISSUE_TEMPLATE/team-application.yml",
    ".github/workflows/pages.yml",
    "data/teams.json",
)

REQUIRED_META_KEYS = (
    "id",
    "name",
    "track",
    "group",
    "number",
    "members",
    "stage",
)

VALID_STAGES = ("idea", "as_is", "to_be", "requirements", "defense", "glossary")
VALID_ROLES = (
    "captain",
    "analyst",
    "architect",
    "requirements",
    "data_researcher",
)
REQUIRED_ROLES = ("captain", "analyst", "architect")
TEAM_SIZE_MIN = 3
TEAM_SIZE_MAX = 5
MAX_ROLE_REPEAT = 2
ID_PATTERN = re.compile(r"^team-[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MIN_LENGTH = 3
NAME_MAX_LENGTH = 60
ID_MAX_LENGTH = 64
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REMOTE_URL_RE = re.compile(r"url\s*=\s*(?P<url>\S+)")


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning"]
    code: str
    path: str
    message: str
    line: int | None = None

    def render(self, *, ci: bool) -> str:
        if ci and self.level == "error":
            location = f" file={self.path}"
            if self.line is not None:
                location += f",line={self.line}"
            return f"::error{location},title={self.code}::{self.message}"
        if ci and self.level == "warning":
            location = f" file={self.path}"
            if self.line is not None:
                location += f",line={self.line}"
            return f"::warning{location},title={self.code}::{self.message}"
        suffix = f":{self.line}" if self.line is not None else ""
        return f"{self.level.upper():7} {self.code:28} {self.path}{suffix} {self.message}"


@dataclass
class Report:
    root: str
    profile: Profile
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "warning"]

    def add(
        self,
        level: Literal["error", "warning"],
        code: str,
        path: str,
        message: str,
        line: int | None = None,
    ) -> None:
        self.findings.append(Finding(level=level, code=code, path=path, message=message, line=line))

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)


def normalize_heading(text: str) -> str:
    """Reduce a heading to a comparable form: case and punctuation insensitive."""
    cleaned = re.sub(r"[`*_]", "", text)
    cleaned = cleaned.rstrip(".。№:;,")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().casefold()


def is_iso_date(value: Any) -> bool:
    """Whether a YAML scalar is a plain `YYYY-MM-DD` date.

    PyYAML resolves an unquoted date to `datetime.date`, so a plain string check
    would silently skip the validation for every correctly written document.
    """
    if isinstance(value, (datetime, date)):
        text = value.isoformat()
        if isinstance(value, datetime):
            text = value.date().isoformat()
        return bool(ISO_DATE_RE.match(text))
    if not isinstance(value, str):
        return False
    return bool(ISO_DATE_RE.match(value))


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Return (raw frontmatter, body, body_start_line)."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None, text, 1
    body_start = text.count("\n", 0, match.end()) + 1
    return match.group(1), text[match.end() :], body_start


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, int, str | None]:
    raw, body, body_start = split_frontmatter(text)
    if raw is None:
        return None, body, body_start, "отсутствует YAML-фронтматтер между строками ---"
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return None, body, body_start, f"не удалось разобрать YAML: {exc}"
    if not isinstance(data, dict):
        return None, body, body_start, "фронтматтер должен быть словарём"
    return data, body, body_start, None


def find_headings(body: str) -> list[tuple[str, int, int, int]]:
    """Return (normalized_text, level, line, column) for every heading."""
    result: list[tuple[str, int, int, int]] = []
    for match in HEADING_RE.finditer(body):
        line = body.count("\n", 0, match.start()) + 1
        result.append((normalize_heading(match.group(2)), len(match.group(1)), line, match.start()))
    return result


def find_section_line(body: str, wanted: str) -> tuple[int, int] | None:
    """Locate a heading by text at any level. Returns (line, level) or None."""
    target = normalize_heading(wanted)
    for text, level, line, _ in find_headings(body):
        if text == target:
            return line, level
    return None


def section_body(body: str, heading_line: int, heading_level: int) -> str:
    """Return the content of a section up to the next heading of same or higher level."""
    heading_re = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
    start = None
    offset = 0
    for _ in range(heading_line):
        offset = body.find("\n", offset) + 1
        start = offset
    boundary = None
    for match in heading_re.finditer(body, start or 0):
        line_no = body.count("\n", 0, match.start()) + 1
        if line_no > heading_line and len(match.group(1)) <= heading_level:
            boundary = match.start()
            break
    if boundary is None:
        return body[start or 0 :]
    return body[start or 0 : boundary]


def is_meaningful(text: str) -> bool:
    """Whether a section holds something beyond markup scaffolding.

    Headings and comments are markup, not content: a section that contains
    only subheadings is as empty as one that contains nothing at all.
    """
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        if set(line) <= {"|", "-", " ", ":"}:
            continue
        lines.append(line)
    if not lines:
        return False
    return not any(pattern.search(text) for _, pattern in PLACEHOLDER_PATTERNS)


def check_file_presence(root: Path, report: Report, required: Iterable[str]) -> None:
    for relative in required:
        target = root / relative
        if not target.is_file():
            report.add(
                "error",
                "file.missing",
                relative,
                "обязательный файл отсутствует",
            )
        elif target.stat().st_size == 0:
            report.add("error", "file.empty", relative, "файл пуст")


def comment_line_numbers(text: str) -> set[int]:
    """Line numbers covered by an HTML comment, inclusive of the fences."""
    covered: set[int] = set()
    for match in HTML_COMMENT_RE.finditer(text):
        first = text.count("\n", 0, match.start()) + 1
        last = text.count("\n", 0, match.end()) + 1
        covered.update(range(first, last + 1))
    return covered


def check_placeholders(root: Path, report: Report, relative: str) -> None:
    """Report leftover scaffolding.

    `ЗАПОЛНИТЕ` and `{{ ... }}` are instructions to the author, so their
    occurrence inside a comment is not a leftover. A `TODO` comment is the
    opposite: it is exactly the kind of unfinished work we must catch, so it
    is searched in the raw text.
    """
    target = root / relative
    if not target.is_file():
        return
    text = target.read_text(encoding="utf-8")
    commented = comment_line_numbers(text)

    for number, raw_line in enumerate(text.splitlines(), 1):
        for code, pattern in PLACEHOLDER_PATTERNS:
            if code != "todo_marker" and number in commented:
                continue
            match = pattern.search(raw_line)
            if match is None:
                continue
            report.add(
                "error",
                f"placeholder.{code}",
                relative,
                f"осталась заготовка: {match.group(0)[:60]}",
                number,
            )
            break


def check_document(root: Path, report: Report, relative: str, profile: Profile) -> None:
    target = root / relative
    if not target.is_file():
        return

    spec = DOCUMENT_SPECS[relative]
    data, body, body_start, error = parse_frontmatter(target.read_text(encoding="utf-8"))

    if error is not None:
        report.add("error", "frontmatter.invalid", relative, f"фронтматтер: {error}", 1)
        data = None

    if data is not None:
        for key in spec["frontmatter"]:
            if key not in data:
                report.add("error", "frontmatter.key", relative, f"нет ключа `{key}`", 1)
        authors = data.get("authors")
        if authors is not None and not isinstance(authors, list):
            report.add("error", "frontmatter.type", relative, "`authors` должен быть списком", 1)
        updated = data.get("updated")
        if updated is not None and not is_iso_date(updated):
            report.add(
                "error",
                "frontmatter.date",
                relative,
                "`updated` должен быть датой в формате YYYY-MM-DD",
                1,
            )

    for section in spec["sections"]:
        location = find_section_line(body, section)
        if location is None:
            report.add(
                "error",
                "section.missing",
                relative,
                f"нет обязательной секции `## {section}`",
            )
            continue
        line, level = location
        if level > 2:
            report.add(
                "warning",
                "section.level",
                relative,
                f"секция `{section}` начинается с уровня h{level}, ожидался h2",
                body_start + line - 1,
            )
        if section in {"Human-in-the-Loop"} and profile == "team":
            content = section_body(body, line, level)
            if not is_meaningful(content):
                report.add(
                    "error",
                    "section.empty",
                    relative,
                    f"секция `{section}` пуста или состоит только из заготовок",
                    body_start + line - 1,
                )

    if spec["mermaid"]:
        blocks = MERMAID_RE.findall(body)
        if not blocks:
            report.add("error", "diagram.missing", relative, "нет диаграммы ```mermaid```")
        else:
            empty = [index for index, block in enumerate(blocks, 1) if not block.strip()]
            if empty:
                report.add(
                    "error",
                    "diagram.empty",
                    relative,
                    f"пустые блоки ```mermaid```: {', '.join(str(i) for i in empty)}",
                )

    if profile == "team":
        check_placeholders(root, report, relative)


def repository_name(root: Path) -> str | None:
    """Read the repository name from the `origin` remote, if there is one.

    Only used by the `team` profile, and only to cross-check `data/meta.yml`
    against the directory the team actually works in. Returns `None` when the
    tree is not a git checkout — a scaffolded copy on a laptop, for instance —
    because a check that cannot run must not be reported as a failure.
    """
    config = root / ".git" / "config"
    if not config.is_file():
        return None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped in {'[remote "origin"]'}
            continue
        if not in_origin:
            continue
        match = REMOTE_URL_RE.match(stripped)
        if match is None:
            continue
        url = match.group("url").removesuffix(".git").rstrip("/")
        return url.rsplit("/", 1)[-1] or None
    return None


def check_meta(root: Path, report: Report) -> None:
    target = root / "data/meta.yml"
    if not target.is_file():
        return

    data, error = load_yaml_mapping(target)
    if error is not None:
        report.add("error", "meta.invalid", "data/meta.yml", f"YAML: {error}", 1)
        return
    if data is None:
        return

    for key in REQUIRED_META_KEYS:
        if key not in data or data[key] in (None, "", [], {}):
            report.add("error", "meta.key", "data/meta.yml", f"нет обязательного ключа `{key}`", 1)

    team_id = data.get("id")
    if isinstance(team_id, str) and team_id:
        if not ID_PATTERN.match(team_id):
            report.add(
                "error",
                "meta.id_format",
                "data/meta.yml",
                f"`id` должен иметь вид team-<трек>-<NN>, получено `{team_id}`",
            )
        if len(team_id) > ID_MAX_LENGTH:
            report.add("error", "meta.id_length", "data/meta.yml", f"`id` длиннее {ID_MAX_LENGTH}")
        # The repository is created as team-<track>-<NN> and cannot be renamed,
        # so an `id` that disagrees with the directory name means the file
        # still describes somebody else's team. Catching it here fails the
        # team's own pull request instead of publishing the mismatch to the
        # showcase. The template profile is exempt on purpose: the reference
        # repository is called `team-template` and its `id` is a placeholder.
        current = repository_name(root)
        if current and team_id != current:
            report.add(
                "error",
                "meta.id_mismatch",
                "data/meta.yml",
                f"`id` ({team_id}) не совпадает с именем репозитория ({current}). "
                "Значение проставляется воркфлоу provision-team.yml по заявке команды.",
            )

    name = data.get("name")
    if isinstance(name, str) and name and not NAME_MIN_LENGTH <= len(name) <= NAME_MAX_LENGTH:
        report.add(
            "error",
            "meta.name_length",
            "data/meta.yml",
            f"`name` должен быть от {NAME_MIN_LENGTH} до {NAME_MAX_LENGTH} символов",
        )

    stage = data.get("stage")
    if isinstance(stage, str) and stage and stage not in VALID_STAGES:
        report.add(
            "error",
            "meta.stage",
            "data/meta.yml",
            f"неизвестный этап `{stage}`, допустимо: {', '.join(VALID_STAGES)}",
        )

    members = data.get("members")
    if not isinstance(members, list):
        report.add("error", "meta.members", "data/meta.yml", "`members` должен быть списком", 1)
        return

    if not TEAM_SIZE_MIN <= len(members) <= TEAM_SIZE_MAX:
        report.add(
            "error",
            "meta.team_size",
            "data/meta.yml",
            f"в команде {len(members)} участников, требуется от {TEAM_SIZE_MIN} до {TEAM_SIZE_MAX}",
        )

    logins: list[str] = []
    roles: list[str] = []
    for index, member in enumerate(members, 1):
        if not isinstance(member, dict):
            report.add(
                "error",
                "meta.member",
                "data/meta.yml",
                f"участник {index}: ожидается словарь с полями login и role",
            )
            continue
        login = member.get("login")
        role = member.get("role")
        if not isinstance(login, str) or not login.strip():
            report.add("error", "meta.member", "data/meta.yml", f"участник {index}: нет `login`")
        else:
            logins.append(login)
        if not isinstance(member.get("name"), str) or not str(member.get("name")).strip():
            report.add(
                "warning",
                "meta.member_name",
                "data/meta.yml",
                f"участник {index}: не заполнено `name`, команда не попадёт в состав на витрине",
            )
        if not isinstance(role, str) or not role.strip():
            report.add("error", "meta.member", "data/meta.yml", f"участник {index}: нет `role`")
        else:
            roles.append(role)
            if role not in VALID_ROLES:
                report.add(
                    "error",
                    "meta.role",
                    "data/meta.yml",
                    f"участник {index}: неизвестная роль `{role}`, "
                    f"допустимо: {', '.join(VALID_ROLES)}",
                )

    duplicates = sorted({login for login in logins if logins.count(login) > 1})
    if duplicates:
        report.add(
            "error",
            "meta.duplicate",
            "data/meta.yml",
            f"повторяющиеся участники: {', '.join(duplicates)}",
        )

    missing_roles = [role for role in REQUIRED_ROLES if role not in roles]
    if missing_roles:
        report.add(
            "error",
            "meta.required_role",
            "data/meta.yml",
            f"в команде нет обязательных ролей: {', '.join(missing_roles)}",
        )

    for role in set(roles):
        count = roles.count(role)
        if count > MAX_ROLE_REPEAT:
            report.add(
                "error",
                "meta.role_repeat",
                "data/meta.yml",
                f"роль `{role}` назначена {count} раз(а), максимум {MAX_ROLE_REPEAT}",
            )


def load_yaml_mapping(target: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, str(exc).splitlines()[0]
    if not isinstance(data, dict):
        return None, "ожидается словарь в корне файла"
    return data, None


def check_coordination(root: Path, report: Report) -> None:
    check_file_presence(root, report, REQUIRED_COORDINATION_FILES)
    target = root / "data/teams.json"
    if target.is_file():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.add(
                "error", "registry.invalid", "data/teams.json", f"JSON: {exc.msg}", exc.lineno
            )
        else:
            if not isinstance(payload, dict):
                report.add("error", "registry.invalid", "data/teams.json", "ожидается объект")
            elif not isinstance(payload.get("teams"), list):
                report.add(
                    "error",
                    "registry.invalid",
                    "data/teams.json",
                    "нет массива `teams`",
                )


def validate(root: Path, profile: Profile) -> Report:
    report = Report(root=str(root), profile=profile)

    if not root.is_dir():
        report.add("error", "root.missing", str(root), "каталог не найден")
        return report

    if profile == "team":
        check_file_presence(root, report, REQUIRED_TEAM_PATHS)
        for relative in REQUIRED_TEAM_FILES:
            check_document(root, report, relative, profile)
        for relative in OPTIONAL_TEAM_FILES:
            if (root / relative).is_file():
                check_document(root, report, relative, profile)
            else:
                report.add("warning", "file.optional", relative, "необязательный файл отсутствует")
        check_meta(root, report)
        return report

    if profile == "template":
        check_file_presence(root, report, REQUIRED_TEAM_PATHS)
        for relative in REQUIRED_TEAM_FILES:
            target = root / relative
            if not target.is_file():
                continue
            spec = DOCUMENT_SPECS[relative]
            data, body, _, error = parse_frontmatter(target.read_text(encoding="utf-8"))
            if error is not None:
                report.add("error", "frontmatter.invalid", relative, f"фронтматтер: {error}", 1)
            elif data is not None:
                for key in spec["frontmatter"]:
                    if key not in data:
                        report.add(
                            "error",
                            "frontmatter.key",
                            relative,
                            f"нет ключа `{key}` в эталоне",
                            1,
                        )
            for section in spec["sections"]:
                if find_section_line(body, section) is None:
                    report.add(
                        "error",
                        "section.missing",
                        relative,
                        f"нет обязательной секции `## {section}`",
                    )
            if spec["mermaid"]:
                blocks = MERMAID_RE.findall(body)
                if any(not block.strip() for block in blocks):
                    report.add("error", "diagram.empty", relative, "блок ```mermaid``` пуст")
        check_meta(root, report)
        return report

    check_coordination(root, report)
    return report


def scope_for(changed: Iterable[str]) -> Scope:
    """How much of the `team` profile a set of changed paths can possibly fail.

    `full` when a document, the README or the validator itself moved, `meta`
    when only `data/` moved, `none` when nothing the profile reads changed.
    Windows separators are accepted because a rehearsal on a laptop compares
    paths typed by hand, while a runner reports them from `git diff`.

    An empty list means "nothing changed", and the caller decides what to do
    with that: a pull request with an empty diff is a failure to understand the
    request and is checked in full, while a branch that was created a moment ago
    has nothing to compare against and is not checked at all.
    """
    paths = [item.strip().replace("\\", "/") for item in changed]
    paths = [item for item in paths if item]
    if any(item in FULL_SCOPE_FILES or item.startswith(FULL_SCOPE_PREFIXES) for item in paths):
        return "full"
    if any(item.startswith(META_SCOPE_PREFIXES) for item in paths):
        return "meta"
    return "none"


def read_changed_paths(source: str) -> list[str]:
    """Changed paths from a file with one path per line, or from stdin for `-`."""
    if source == "-":
        return sys.stdin.read().splitlines()
    return Path(source).read_text(encoding="utf-8").splitlines()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_docs",
        description="Проверка артефактов практики по профилю.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="каталог с документами (по умолчанию текущий)",
    )
    parser.add_argument(
        "--profile",
        choices=("team", "template", "coordination"),
        default="team",
        help="что проверяем: артефакты команды, эталон или координационный репозиторий",
    )
    parser.add_argument("--ci", action="store_true", help="аннотации для GitHub Actions")
    parser.add_argument(
        "--scope",
        metavar="FILE",
        default=None,
        help="определить круг проверки по списку изменённых файлов и вывести full, meta или none",
    )
    parser.add_argument("--json", action="store_true", help="вывод в виде JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="предупреждения считаются ошибками",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # `--scope` отвечает на вопрос «что имеет смысл проверять», а не «что не так»,
    # поэтому входных документов ему не нужно: список изменённых файлов приходит
    # из `git diff` на runner.
    if args.scope is not None:
        print(scope_for(read_changed_paths(args.scope)))
        return 0

    report = validate(Path(args.root).resolve(), args.profile)

    if args.json:
        payload = {
            "root": report.root,
            "profile": report.profile,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "findings": [asdict(item) for item in report.findings],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for finding in report.findings:
            print(finding.render(ci=args.ci))
        summary = f"{len(report.errors)} ошибок, {len(report.warnings)} предупреждений"
        print(f"\n{report.root} [{report.profile}]: {summary}")

    failed = bool(report.errors) or (args.strict and bool(report.warnings))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
