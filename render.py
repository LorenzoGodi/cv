#!/usr/bin/env python3
"""Render the CVs in this repository from YAML to PDF.

Reads a CV YAML file from src/, renders it through the customized sb2nov
Jinja2/LaTeX templates (computing localized date strings, header connections,
LaTeX escaping, and Markdown-to-LaTeX conversion), and compiles the result
with pdflatex.

Usage:
    python render.py src/LorenzoGodi_CV_it.yaml [--latex-path X.tex]
                     [--pdf-path X.pdf] [--no-pdf] [--latex-command pdflatex]

Output paths default to the `render_settings` values in the YAML file,
falling back to the input file's stem in the current directory.
"""

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date as Date

import jinja2
import yaml

# ======================================================================
# Locale and defaults
# ======================================================================

DEFAULT_LOCALE = {
    "phone_number_format": "national",
    "date_style": "MONTH_ABBREVIATION YEAR",
    "month": "month",
    "months": "months",
    "year": "year",
    "years": "years",
    "present": "present",
    "to": "–",  # en dash
    "abbreviations_for_months": [
        "Jan", "Feb", "Mar", "Apr", "May", "June",
        "July", "Aug", "Sept", "Oct", "Nov", "Dec",
    ],
    "full_names_of_months": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
}

SOCIAL_NETWORK_URLS = {
    "LinkedIn": "https://linkedin.com/in/",
    "GitHub": "https://github.com/",
    "GitLab": "https://gitlab.com/",
    "Instagram": "https://instagram.com/",
    "ORCID": "https://orcid.org/",
    "StackOverflow": "https://stackoverflow.com/users/",
    "ResearchGate": "https://researchgate.net/profile/",
    "YouTube": "https://youtube.com/@",
    "Google Scholar": "https://scholar.google.com/citations?user=",
}

SOCIAL_NETWORK_ICONS = {
    "LinkedIn": "\\faLinkedinIn",
    "GitHub": "\\faGithub",
    "GitLab": "\\faGitlab",
    "Instagram": "\\faInstagram",
    "Mastodon": "\\faMastodon",
    "ORCID": "\\faOrcid",
    "StackOverflow": "\\faStackOverflow",
    "ResearchGate": "\\faResearchgate",
    "YouTube": "\\faYoutube",
    "Google Scholar": "\\faGraduationCap",
}

# Top-level template that assembles preamble, header, and sections:
MAIN_TEMPLATE = """<<preamble>>

\\begin{document}
    <<header|indent(4)>>

((* for section_beginning, entries, section_ending in sections*))
    <<section_beginning|indent(4)>>

    ((* for entry in entries *))
        <<entry|indent(8)>>

    ((* endfor *))

    <<section_ending|indent(4)>>
((* endfor *))

\\end{document}"""


# ======================================================================
# String transformations (LaTeX escaping and Markdown conversion)
# ======================================================================

def escape_latex_characters(latex_string, strict=True):
    """Escape special LaTeX characters, leaving [text](url) link URLs alone."""
    escape_characters = {
        "#": "\\#",
        "%": "\\%",
        "&": "\\&",
        "~": "\\textasciitilde{}",
    }
    strict_escape_characters = {
        "$": "\\$",
        "_": "\\_",
        "^": "\\textasciicircum{}",
    }
    if strict:
        escape_characters.update(strict_escape_characters)

    translation_map = str.maketrans(escape_characters)
    strict_translation_map = str.maketrans(strict_escape_characters)

    # Don't escape URLs, but escape link placeholders strictly:
    links = re.findall(r"\[(.*?)\]\((.*?)\)", latex_string)
    new_links = []
    for i, (placeholder, url) in enumerate(links):
        escaped_placeholder = placeholder.translate(strict_translation_map)
        escaped_placeholder = escaped_placeholder.translate(translation_map)
        latex_string = latex_string.replace(
            f"[{placeholder}]({url})", f"!!-link{i}-!!"
        )
        new_links.append(f"[{escaped_placeholder}]({url})")

    latex_string = latex_string.translate(translation_map)

    for i, new_link in enumerate(new_links):
        latex_string = latex_string.replace(f"!!-link{i}-!!", new_link)

    return latex_string


def markdown_to_latex(markdown_string):
    """Convert Markdown links, bold, and italic to LaTeX commands."""
    for link_text, link_url in re.findall(r"\[([^\]\[]*)\]\((.*?)\)", markdown_string):
        markdown_string = markdown_string.replace(
            f"[{link_text}]({link_url})",
            "\\href{" + link_url + "}{" + link_text + "}",
        )
    for bold_text in re.findall(r"\*\*(.+?)\*\*", markdown_string):
        markdown_string = markdown_string.replace(
            f"**{bold_text}**", "\\textbf{" + bold_text + "}"
        )
    for italic_text in re.findall(r"\*(.+?)\*", markdown_string):
        markdown_string = markdown_string.replace(
            f"*{italic_text}*", "\\textit{" + italic_text + "}"
        )
    return markdown_string


def transform_string(value):
    return markdown_to_latex(escape_latex_characters(value, strict=False))


def revert_nested_latex_style_commands(latex_string):
    """Replace nested \\textbf/\\textit/\\underline with \\textnormal."""
    for command in ["textbf", "textit", "underline"]:
        while True:
            nested_commands = re.findall(
                rf"\\{command}{{[^}}]*?(\\{command}{{.*?}})", latex_string
            )
            if not nested_commands:
                break
            for nested_command in nested_commands:
                latex_string = latex_string.replace(
                    nested_command, nested_command.replace(command, "textnormal")
                )
    return latex_string


def replace_placeholders_with_actual_values(text, placeholders):
    for placeholder, value in placeholders.items():
        text = text.replace(placeholder, str(value))
    return text


def make_matched_part_something(value, something, match_str=None):
    if match_str is None:
        value = f"\\{something}{{{value}}}"
    elif match_str in value and match_str != "":
        value = value.replace(match_str, f"\\{something}{{{match_str}}}")
    return value


def divide_length_by(length, divider):
    """Divide a LaTeX length like "0.8 cm" by a number."""
    value = re.search(r"\d+\.?\d*", length)
    if value is None:
        raise ValueError(f"Invalid length {length}!")
    unit = re.findall(r"[^\d\.\s]+", length)[0]
    return str(float(value.group()) / divider) + " " + unit


# ======================================================================
# Date computations
# ======================================================================

def get_date_object(date):
    if isinstance(date, int):
        return Date.fromisoformat(f"{date}-01-01")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return Date.fromisoformat(date)
    if re.fullmatch(r"\d{4}-\d{2}", date):
        return Date.fromisoformat(f"{date}-01")
    if re.fullmatch(r"\d{4}", date):
        return Date.fromisoformat(f"{date}-01-01")
    if date == "present":
        return Date.today()
    raise ValueError(
        f"{date} is not a valid date! Use YYYY-MM-DD, YYYY-MM, or YYYY format."
    )


def format_date(date, locale, date_style=None):
    """Format a date object according to the (localized) date style."""
    month = date.month
    year = str(date.year)
    placeholders = {
        "FULL_MONTH_NAME": locale["full_names_of_months"][month - 1],
        "MONTH_ABBREVIATION": locale["abbreviations_for_months"][month - 1],
        "MONTH_IN_TWO_DIGITS": f"{month:02d}",
        "YEAR_IN_TWO_DIGITS": year[-2:],
        "MONTH": str(month),
        "YEAR": year,
    }
    if date_style is None:
        date_style = locale["date_style"]
    for placeholder, value in placeholders.items():
        date_style = date_style.replace(placeholder, value)
    return date_style


def compute_date_string(entry, locale):
    """Compute the "gen 2025 – oggi" style date string for an entry."""
    date = entry.get("date")
    start_date = entry.get("start_date")
    end_date = entry.get("end_date")

    if date is not None:
        if isinstance(date, int):
            return str(date)
        try:
            return format_date(get_date_object(date), locale)
        except ValueError:
            return str(date)  # custom date string

    if start_date is not None and end_date is not None:
        if isinstance(start_date, int):
            start = str(start_date)
        else:
            start = format_date(get_date_object(start_date), locale)

        if end_date == "present":
            end = locale["present"]
        elif isinstance(end_date, int):
            end = str(end_date)
        else:
            end = format_date(get_date_object(end_date), locale)

        return f"{start} {locale['to']} {end}"

    return ""


# ======================================================================
# Data model
# ======================================================================

class AttrDict:
    """Read-only attribute access over nested dictionaries (for `design`)."""

    def __init__(self, data, path="design"):
        self._data = data
        self._path = path

    def __getattr__(self, key):
        if key not in self._data:
            raise KeyError(f"Missing key '{self._path}.{key}' in the input file!")
        value = self._data[key]
        if isinstance(value, dict):
            return AttrDict(value, f"{self._path}.{key}")
        return value


class Color:
    """Hex color with pydantic-style as_rgb_tuple(), used by the preamble."""

    def __init__(self, hex_string):
        hex_string = hex_string.lstrip("#")
        self.rgb = tuple(int(hex_string[i : i + 2], 16) for i in (0, 2, 4))

    def as_rgb_tuple(self):
        return self.rgb


class Entry:
    """An entry whose missing fields render as empty strings."""

    def __init__(self, fields):
        self.__dict__.update(fields)

    def __getattr__(self, key):
        return ""


class CV:
    def __init__(self, name, connections):
        self.name = name
        self.connections = connections


def format_phone_number(phone):
    """Format "***REMOVED***" nationally as "***REMOVED***".

    The phone number must be written as +COUNTRYCODE-XXX-XXX-XXXX in the YAML.
    """
    return re.sub(r"^\+\d+-", "", phone).replace("-", " ")


def build_connections(cv_data):
    """Build the list of connections shown in the header."""
    connections = []
    if cv_data.get("location"):
        connections.append({
            "latex_icon": "\\faMapMarker*",
            "url": None,
            "clean_url": None,
            "placeholder": cv_data["location"],
        })
    if cv_data.get("email"):
        connections.append({
            "latex_icon": "\\faEnvelope[regular]",
            "url": f"mailto:{cv_data['email']}",
            "clean_url": cv_data["email"],
            "placeholder": cv_data["email"],
        })
    if cv_data.get("phone"):
        phone_placeholder = format_phone_number(cv_data["phone"])
        connections.append({
            "latex_icon": "\\faPhone*",
            "url": f"tel:{cv_data['phone']}",
            "clean_url": phone_placeholder,
            "placeholder": phone_placeholder,
        })
    if cv_data.get("website"):
        website = cv_data["website"]
        clean = website.replace("https://", "").replace("http://", "").rstrip("/")
        connections.append({
            "latex_icon": "\\faLink",
            "url": website,
            "clean_url": clean,
            "placeholder": clean,
        })
    for social_network in cv_data.get("social_networks") or []:
        network = social_network["network"]
        username = social_network["username"]
        url = SOCIAL_NETWORK_URLS[network] + username
        connections.append({
            "latex_icon": SOCIAL_NETWORK_ICONS[network],
            "url": url,
            "clean_url": url.replace("https://", "").rstrip("/"),
            "placeholder": username,
        })
    return connections


def detect_entry_type(entry):
    if isinstance(entry, str):
        return "TextEntry"
    if "bullet" in entry:
        return "BulletEntry"
    if "label" in entry or "details" in entry:
        return "OneLineEntry"
    if "institution" in entry:
        return "EducationEntry"
    if "company" in entry or "position" in entry:
        return "ExperienceEntry"
    if "name" in entry:
        return "NormalEntry"
    raise ValueError(f"Couldn't detect the entry type of {entry}!")


TITLE_LOWERCASE_WORDS = {
    "a", "and", "as", "at", "but", "by", "for", "from", "if", "in", "into",
    "like", "near", "nor", "of", "off", "on", "onto", "or", "over", "so",
    "than", "that", "to", "upon", "when", "with", "yet",
}


def section_key_to_title(key):
    """Convert a section key like "esperienza professionale" to a title."""
    return " ".join(
        word.capitalize()
        if word.islower() and word not in TITLE_LOWERCASE_WORDS
        else word
        for word in key.replace("_", " ").split(" ")
    )


def build_sections(sections_input, locale):
    """Convert the input sections to (title, entry_type, entries) tuples."""
    sections = []
    date_fields = {"start_date", "end_date", "date"}
    for key, raw_entries in sections_input.items():
        entry_type = detect_entry_type(raw_entries[0])
        entries = []
        for raw_entry in raw_entries:
            if isinstance(raw_entry, str):
                entries.append(transform_string(raw_entry))
                continue
            fields = {}
            for field, value in raw_entry.items():
                if isinstance(value, Date):  # YYYY-MM-DD parsed by YAML
                    value = value.isoformat()
                if isinstance(value, str) and field not in date_fields:
                    value = transform_string(value)
                elif isinstance(value, list):
                    value = [
                        transform_string(v) if isinstance(v, str) else v
                        for v in value
                    ]
                fields[field] = value
            fields["date_string"] = compute_date_string(fields, locale)
            # Drop None fields so they render as "" instead of "None":
            entries.append(Entry({k: v for k, v in fields.items() if v is not None}))
        sections.append((section_key_to_title(key), entry_type, entries))
    return sections


# ======================================================================
# Templating
# ======================================================================

def setup_jinja2_environment(templates_directory):
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(templates_directory),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.block_start_string = "((*"
    environment.block_end_string = "*))"
    environment.variable_start_string = "<<"
    environment.variable_end_string = ">>"
    environment.comment_start_string = "((#"
    environment.comment_end_string = "#))"

    environment.filters["divide_length_by"] = divide_length_by
    environment.filters["escape_latex_characters"] = escape_latex_characters
    environment.filters["replace_placeholders_with_actual_values"] = (
        replace_placeholders_with_actual_values
    )
    environment.filters["make_it_something"] = make_matched_part_something
    environment.filters["make_it_bold"] = (
        lambda value, match_str=None: make_matched_part_something(value, "textbf", match_str)
    )
    environment.filters["make_it_italic"] = (
        lambda value, match_str=None: make_matched_part_something(value, "textit", match_str)
    )
    environment.filters["make_it_underlined"] = (
        lambda value, match_str=None: make_matched_part_something(value, "underline", match_str)
    )
    environment.filters["make_it_nolinebreak"] = (
        lambda value, match_str=None: make_matched_part_something(value, "mbox", match_str)
    )
    return environment


def generate_latex(input_data, templates_directory):
    """Generate the full LaTeX code from the parsed YAML data."""
    locale = dict(DEFAULT_LOCALE)
    locale.update(input_data.get("locale_catalog") or {})

    cv_data = input_data["cv"]
    design_data = dict(input_data["design"])
    design_data["color"] = Color(design_data["color"])

    cv = CV(cv_data.get("name"), build_connections(cv_data))
    design = AttrDict(design_data)
    sections = build_sections(cv_data["sections"], locale)
    today = format_date(Date.today(), locale, date_style="FULL_MONTH_NAME YEAR")

    environment = setup_jinja2_environment(templates_directory)
    theme = design_data["theme"]

    def template(name, **kwargs):
        result = environment.get_template(f"{theme}/{name}.j2.tex").render(
            cv=cv, design=design, today=today, **kwargs
        )
        return revert_nested_latex_style_commands(result)

    preamble = template("Preamble")
    header = template("Header")
    rendered_sections = []
    for section_title, entry_type, entries in sections:
        rendered_sections.append((
            template(
                "SectionBeginning", section_title=section_title, entry_type=entry_type
            ),
            [
                template(
                    entry_type,
                    entry=entry,
                    section_title=section_title,
                    entry_type=entry_type,
                    is_first_entry=(i == 0),
                )
                for i, entry in enumerate(entries)
            ],
            template(
                "SectionEnding", section_title=section_title, entry_type=entry_type
            ),
        ))

    return environment.from_string(MAIN_TEMPLATE).render(
        preamble=preamble, header=header, sections=rendered_sections
    )


# ======================================================================
# PDF compilation
# ======================================================================

def compile_pdf(latex_code, tex_name, latex_command):
    """Compile the LaTeX code with pdflatex and return the PDF bytes."""
    if shutil.which(latex_command) is None:
        sys.exit(
            f"Error: '{latex_command}' not found. Install a LaTeX distribution,"
            " use --latex-command, or pass --no-pdf to skip PDF generation."
        )
    with tempfile.TemporaryDirectory() as build_directory:
        tex_path = pathlib.Path(build_directory) / tex_name
        tex_path.write_text(latex_code, encoding="utf-8")
        command = [
            latex_command,
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex_path.name,
        ]
        for _ in range(2):  # second pass fixes references if needed
            result = subprocess.run(
                command,
                cwd=build_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            output = result.stdout.decode("utf-8", errors="replace")
            if result.returncode != 0:
                log_tail = "\n".join(output.splitlines()[-30:])
                sys.exit(f"Error: {latex_command} failed:\n{log_tail}")
            if "Rerun to get" not in output:
                break
        return tex_path.with_suffix(".pdf").read_bytes()


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Render a CV from a YAML input file."
    )
    parser.add_argument("input_file", help="the YAML input file")
    parser.add_argument("--latex-path", help="output path of the .tex file")
    parser.add_argument("--pdf-path", help="output path of the .pdf file")
    parser.add_argument("--no-pdf", action="store_true", help="skip PDF generation")
    parser.add_argument(
        "--latex-command", default="pdflatex", help="LaTeX compiler to use"
    )
    arguments = parser.parse_args()

    input_path = pathlib.Path(arguments.input_file)
    input_data = yaml.safe_load(input_path.read_text(encoding="utf-8"))

    # The phone number is kept out of the repository: it is injected through
    # the CV_PHONE environment variable (a repository secret in CI). Without
    # it, the CV is rendered without the phone connection in the header.
    if os.environ.get("CV_PHONE"):
        input_data["cv"]["phone"] = os.environ["CV_PHONE"]

    # Output paths: CLI arguments win over render_settings in the YAML file:
    settings = input_data.get("render_settings") or {}
    latex_path = pathlib.Path(
        arguments.latex_path or settings.get("latex_path") or f"{input_path.stem}.tex"
    )
    pdf_path = pathlib.Path(
        arguments.pdf_path or settings.get("pdf_path") or f"{input_path.stem}.pdf"
    )

    latex_code = generate_latex(input_data, templates_directory=input_path.parent)
    latex_path.write_text(latex_code, encoding="utf-8")
    print(f"Generated {latex_path}")

    if not arguments.no_pdf:
        pdf_bytes = compile_pdf(latex_code, latex_path.name, arguments.latex_command)
        pdf_path.write_bytes(pdf_bytes)
        print(f"Generated {pdf_path}")


if __name__ == "__main__":
    main()
