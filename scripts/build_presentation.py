"""Build the project presentation (editable .pptx) and its speaker notes, with python-pptx.

    pip install python-pptx            (MIT licence, runs locally, no account, no network)
    python scripts/build_presentation.py

Writes:
  presentation/payment_ops_intelligence.pptx   the deck: native text, shapes, tables and charts,
                                               so every element can be edited in PowerPoint
  docs/presentation_notes.md                   the same speaker notes, for reading or printing

Every number in the deck comes from a run recorded in docs/evaluation.md (or from your own
pytest and evaluation output); the source of each is noted next to it below. Change a
number here, not in PowerPoint, and rebuild, so the deck and the notes stay in step.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
OUT_DECK = ROOT / "presentation" / "payment_ops_intelligence.pptx"
OUT_NOTES = ROOT / "docs" / "presentation_notes.md"

# ---- theme --------------------------------------------------------------------------------------
INK = RGBColor(0x0F, 0x2E, 0x35)        # dominant: deep ink-teal (title/closing backgrounds, headings)
INK_SOFT = RGBColor(0x1E, 0x4A, 0x52)   # cards on dark slides
TEXT = RGBColor(0x1C, 0x2B, 0x30)       # body text on light slides
MUTED = RGBColor(0x5B, 0x6B, 0x70)      # captions
PANEL = RGBColor(0xEE, 0xF3, 0xF4)      # light card fill
LINE = RGBColor(0xC9, 0xD5, 0xD8)       # hairlines, gridlines
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT = RGBColor(0xEB, 0x68, 0x34)     # the one accent: the dashboard's KESTREL colour (the incident)
GOOD = RGBColor(0x1E, 0x8E, 0x66)       # passes / kept
ICE = RGBColor(0xBF, 0xD7, 0xDC)        # secondary text on dark slides
HEAD_FONT, BODY_FONT = "Cambria", "Calibri"

W, H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.6)
TITLE_TOP, TITLE_H = Inches(0.45), Inches(0.9)
CONTENT_TOP = Inches(1.6)


@dataclass
class Notes:
    title: str
    text: str


NOTES: list[Notes] = []


# ---- helpers ------------------------------------------------------------------------------------

def _emu(*values):
    """Whole EMUs. Layout arithmetic (a width divided by n) gives fractions, and OOXML coordinates
    must be integers: PowerPoint and other readers reject '2205685.17'."""
    return [Emu(int(round(v))) for v in values]


def new_slide(prs, title: str, notes: str, dark: bool = False):
    slide = prs.slides.add_slide(prs.slide_layouts[5])          # "Title Only": a real, editable title
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = INK if dark else WHITE
    t = slide.shapes.title
    t.left, t.top, t.width, t.height = MARGIN, TITLE_TOP, W - 2 * MARGIN, TITLE_H
    tf = t.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = title
    _font(r, HEAD_FONT, 36, WHITE if dark else INK, bold=True)
    slide.notes_slide.notes_text_frame.text = notes
    NOTES.append(Notes(title, notes))
    return slide


def _font(run, face, size, color, bold=False, italic=False):
    f = run.font
    f.name, f.size, f.bold, f.italic = face, Pt(size), bold, italic
    f.color.rgb = color


def text(slide, x, y, w, h, paragraphs, size=16, color=TEXT, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, face=BODY_FONT, space_after=6, name=None):
    """paragraphs: str, or list of str / list of (text, {opts}) runs per paragraph."""
    box = slide.shapes.add_textbox(*_emu(x, y, w, h))
    if name:
        box.name = name
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    paras = [paragraphs] if isinstance(paragraphs, str) else paragraphs
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        runs = [(para, {})] if isinstance(para, str) else para
        for rt, opts in runs:
            r = p.add_run()
            r.text = rt
            _font(r, opts.get("face", face), opts.get("size", size), opts.get("color", color),
                  bold=opts.get("bold", bold), italic=opts.get("italic", False))
    return box


def bullets(slide, x, y, w, h, items, size=16, color=TEXT, name=None):
    box = text(slide, x, y, w, h, items, size=size, color=color, space_after=8, name=name)
    for p in box.text_frame.paragraphs:                          # real bullets, not a typed "•"
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(Emu(Inches(0.25))))
        pPr.set("indent", str(-Emu(Inches(0.25))))
        for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
            for e in pPr.findall(qn(tag)):
                pPr.remove(e)
        bu = etree.SubElement(pPr, qn("a:buChar"))
        bu.set("char", "•")
    return box


def card(slide, x, y, w, h, fill=PANEL, name=None):
    shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, *_emu(x, y, w, h))
    shp.adjustments[0] = 0.08
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    shp.line.fill.background()
    shp.shadow.inherit = False
    if name:
        shp.name = name
    shp.text_frame.text = ""
    return shp


def badge(slide, x, y, label, fill=ACCENT, color=WHITE, d=Inches(0.55), size=16):
    """The deck's motif: a step number (or short mark) in a filled circle."""
    c = slide.shapes.add_shape(MSO_SHAPE.OVAL, *_emu(x, y, d, d))
    c.fill.solid()
    c.fill.fore_color.rgb = fill
    c.line.fill.background()
    c.shadow.inherit = False
    tf = c.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    _font(r, BODY_FONT, size, color, bold=True)
    return c


def arrow(slide, x1, y1, x2, y2, color=MUTED, width=1.75):
    conn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, *_emu(x1, y1, x2, y2))
    conn.line.color.rgb = color
    conn.line.width = Pt(width)
    ln = conn.line._get_or_add_ln()
    tail = etree.SubElement(ln, qn("a:tailEnd"))
    tail.set("type", "triangle")
    tail.set("w", "med")
    tail.set("len", "med")
    return conn


def stat(slide, x, y, w, value, label, color=INK, label_color=MUTED, size=44):
    text(slide, x, y, w, Inches(0.8), value, size=size, color=color, bold=True, face=HEAD_FONT)
    text(slide, x, y + Inches(0.85), w, Inches(0.6), label, size=13, color=label_color)


def style_chart(chart, percent=True, legend=True, decimals=1):
    chart.font.name = BODY_FONT
    chart.font.size = Pt(12)
    chart.font.color.rgb = TEXT
    chart.has_legend = legend
    if legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(12)
    va, ca = chart.value_axis, chart.category_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = LINE
    va.format.line.fill.background()
    va.tick_labels.font.color.rgb = MUTED
    va.tick_labels.font.size = Pt(11)
    ca.tick_labels.font.color.rgb = TEXT
    ca.tick_labels.font.size = Pt(12)
    ca.format.line.color.rgb = LINE
    plot = chart.plots[0]
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.font.size = Pt(12)
    dl.font.color.rgb = TEXT
    dl.number_format = ("0." + "0" * decimals + '"%"') if percent else "0"
    dl.number_format_is_linked = False
    dl.position = XL_LABEL_POSITION.OUTSIDE_END
    va.tick_labels.number_format = '0"%"' if percent else "0"
    va.tick_labels.number_format_is_linked = False


def color_series(chart, colors):
    for s, c in zip(chart.plots[0].series, colors):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = c


def table(slide, x, y, w, rows, col_widths, header_fill=INK, size=13, row_h=Inches(0.42)):
    shape = slide.shapes.add_table(len(rows), len(rows[0]), *_emu(x, y, w, row_h * len(rows)))
    tbl = shape.table
    for i, cw in enumerate(col_widths):
        tbl.columns[i].width = _emu(cw)[0]
    for r, row in enumerate(rows):
        tbl.rows[r].height = _emu(row_h)[0]
        for c, value in enumerate(row):
            cell = tbl.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = header_fill if r == 0 else (PANEL if r % 2 == 0 else WHITE)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if c == 0 else PP_ALIGN.CENTER
            run = p.add_run()
            run.text = value
            _font(run, BODY_FONT, size, WHITE if r == 0 else TEXT, bold=(r == 0 or c == 0))
    return shape


# ---- slides -------------------------------------------------------------------------------------

def s_title(prs):
    s = new_slide(prs, "Payment Operations Intelligence", dark=True, notes=(
        "Good morning. This is a multi-agent AI system that answers one question payment operations teams "
        "ask every week: why did payment failures go up, and what should we do about it? Everything you will "
        "see runs on one Windows laptop: no cloud services, no paid APIs, no API keys. The data is synthetic, "
        "generated with a fixed seed, with an incident hidden in it, so we can check whether the system finds "
        "the truth."))
    s.shapes.title.top = Inches(2.3)
    text(s, MARGIN, Inches(3.3), Inches(11), Inches(1.2),
         "A local, multi-agent AI system for investigating payment failures", size=24, color=ICE)
    for i, (label, value) in enumerate([("agents", "6"), ("tests passing", "435"), ("cloud services", "0")]):
        x = MARGIN + Inches(3.2) * i
        badge(s, x, Inches(5.0), value, fill=ACCENT if i == 0 else INK_SOFT, d=Inches(0.8), size=20)
        text(s, x + Inches(0.95), Inches(5.0), Inches(2.1), Inches(0.8), label, size=18, color=WHITE,
             anchor=MSO_ANCHOR.MIDDLE)
    text(s, MARGIN, Inches(6.6), Inches(12), Inches(0.4),
         "Portfolio project · synthetic data · Python, PostgreSQL, FastAPI, Streamlit, LangGraph, Ollama",
         size=13, color=ICE)


def s_problem(prs):
    s = new_slide(prs, "Why did payment failures go up last week?", notes=(
        "When the failure rate jumps, an analyst has to combine four kinds of evidence by hand: the transactions "
        "themselves, our own deployments, the providers' status notices, and history to know what normal looks "
        "like. That is slow, and it invites the classic mistake of blaming whatever changed just before, the "
        "last deployment, without checking. The dataset here has 492,996 synthetic payments over 90 days across "
        "four payment providers, with one large incident injected: Kestrel timeouts from 23 to 25 September. "
        "There is also a deliberate red herring: a deployment two hours before it."))
    text(s, MARGIN, CONTENT_TOP, Inches(5.6), Inches(1.3),
         "When the failure rate jumps, an analyst has to combine evidence that lives in different places, and "
         "avoid blaming whatever changed last.", size=18)
    sources = [("Transactions", "who failed, where, why"), ("Deployments", "what we changed"),
               ("Provider notices", "what they admitted"), ("History", "what is normal")]
    for i, (head, sub) in enumerate(sources):
        y = Inches(3.15) + Inches(0.95) * i
        badge(s, MARGIN, y, str(i + 1), fill=INK)
        text(s, MARGIN + Inches(0.75), y - Inches(0.02), Inches(4.6), Inches(0.75),
             [[(head, {"bold": True, "size": 17})], [(sub, {"color": MUTED, "size": 14})]], space_after=0)
    card(s, Inches(7.0), CONTENT_TOP, Inches(5.73), Inches(5.2), fill=PANEL, name="dataset card")
    text(s, Inches(7.4), Inches(1.9), Inches(5.0), Inches(0.5), "The test bed (synthetic, seed 42)", size=16,
         color=MUTED, bold=True)
    stat(s, Inches(7.4), Inches(2.45), Inches(2.6), "492,996", "payment attempts")
    stat(s, Inches(10.1), Inches(2.45), Inches(2.4), "90 days", "1 Jul – 28 Sep 2026")
    stat(s, Inches(7.4), Inches(4.0), Inches(2.6), "4", "payment providers")
    stat(s, Inches(10.1), Inches(4.0), Inches(2.4), "1 + 1", "incidents hidden", color=ACCENT)
    text(s, Inches(7.4), Inches(5.65), Inches(5.0), Inches(0.9),
         "The answer key (ground_truth.json) is never read by the application, only by the tests.",
         size=13, color=MUTED)


def s_flow(prs):
    s = new_slide(prs, "One question in, a verified report out", notes=(
        "Here is the whole flow. A question in plain English. The local LLM proposes a plan, which week and which "
        "providers, and code validates it. Three agents then analyse in parallel. The impact agent sizes the "
        "damage. The LLM writes the report from the computed findings only, and every sentence is checked before "
        "anyone sees it. If too much fails, the user gets a clearly labelled template report instead. On the laptop "
        "a full run takes about 17 seconds for a simple question, and 26 seconds is the median across the ten evaluation "
        "questions; almost all of it is the model writing."))
    steps = [("Plan", "LLM reads the question;\ncode validates it"),
             ("Analyse ×3", "transactions, patterns,\nforecast in parallel"),
             ("Size impact", "measured, estimated,\nhypothetical, labelled"),
             ("Write", "LLM writes from\nfindings only"),
             ("Verify", "every sentence and\nnumber checked"),
             ("Report", "LLM report, or a\nlabelled template")]
    n, gap = len(steps), Inches(0.3)
    cw = (W - 2 * MARGIN - gap * (n - 1)) / n
    y = Inches(2.3)
    for i, (head, sub) in enumerate(steps):
        x = MARGIN + (cw + gap) * i
        card(s, x, y, cw, Inches(2.6), fill=PANEL)
        badge(s, x + Inches(0.25), y + Inches(0.3), str(i + 1), fill=ACCENT if i in (0, 3) else INK)
        text(s, x + Inches(0.25), y + Inches(1.05), cw - Inches(0.4), Inches(0.5), head, size=18, bold=True)
        text(s, x + Inches(0.25), y + Inches(1.55), cw - Inches(0.4), Inches(0.9), sub.split("\n"), size=13,
             color=MUTED, space_after=0)
        if i < n - 1:
            arrow(s, x + cw + Inches(0.04), y + Inches(1.3), x + cw + gap - Inches(0.04), y + Inches(1.3))
    text(s, MARGIN, Inches(5.35), Inches(12), Inches(0.5),
         [[("Orange", {"bold": True, "color": ACCENT}), (" = the only two steps where the LLM is used. ", {}),
           ("Everything else is SQL and tested Python.", {"bold": True})]], size=16)
    text(s, MARGIN, Inches(6.0), Inches(12), Inches(0.5),
         "Measured on the development laptop (qwen3:4b, AMD GPU): median 26 s per investigation, "
         "under 0.5 s of it analysis.", size=14, color=MUTED)


def s_principles(prs):
    s = new_slide(prs, "Four rules shaped every design decision", notes=(
        "Four rules drove every design decision. One: local only and free. The configuration refuses any "
        "non-local address, and the test suite blocks outbound network calls. Two: code computes, the LLM writes. "
        "The model never calculates a number. Three: the backend enforces access. Roles are checked on every "
        "API call, and the LLM has no say in it. Four: every claim is checked, including the numbers, the "
        "citations and causal wording."))
    rules = [("Local only, zero cost", "Ollama for the LLM, PostgreSQL in Docker; config rejects non-local "
              "hosts; tests fail on any outbound call."),
             ("Code computes, the LLM writes", "Every number comes from SQL and tested Python. The model only plans "
              "and phrases, and cites the finding behind each sentence."),
             ("The backend enforces access", "Sign-in and role checks on every API route; the LLM never "
              "decides who may see what."),
             ("Every claim is checked", "Citations, numbers, dates, causal and proof wording, and recomputation "
              "of key figures from the database.")]
    cw, ch, gap = Inches(5.9), Inches(2.3), Inches(0.33)
    for i, (head, body) in enumerate(rules):
        x = MARGIN + (cw + gap) * (i % 2)
        y = CONTENT_TOP + (ch + gap) * (i // 2)
        card(s, x, y, cw, ch)
        badge(s, x + Inches(0.3), y + Inches(0.3), str(i + 1), fill=ACCENT if i == 1 else INK)
        text(s, x + Inches(1.05), y + Inches(0.32), cw - Inches(1.35), Inches(0.5), head, size=19, bold=True)
        text(s, x + Inches(1.05), y + Inches(0.9), cw - Inches(1.35), Inches(1.3), body, size=15, color=TEXT)


def s_architecture(prs):
    s = new_slide(prs, "Everything runs on one computer", notes=(
        "The architecture. Everything sits inside the boundary of this one computer. The browser talks to the "
        "Streamlit dashboard, which talks only to the API. The API checks sign-in and roles, then runs the "
        "LangGraph workflow. Agents read the payment data through a read-only database login. History, accounts "
        "and the audit log live in a separate schema, written by a second login that cannot see payment data. "
        "The LLM is Ollama on the local GPU. Nothing crosses the dashed line; the only internet use is downloading "
        "software once, and GitHub CI, which is development tooling, not part of the app."))
    bx, by, bw, bh = MARGIN, CONTENT_TOP, W - 2 * MARGIN, Inches(5.25)
    boundary = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, bx, by, bw, bh)
    boundary.adjustments[0] = 0.04
    boundary.fill.background()
    boundary.line.color.rgb = MUTED
    boundary.line.width = Pt(1.5)
    boundary.line.dash_style = 4          # dashed: the edge of the computer
    boundary.shadow.inherit = False
    boundary.name = "this computer"
    text(s, bx + Inches(0.3), by + Inches(0.15), Inches(6), Inches(0.4),
         "This computer (127.0.0.1 only)", size=14, color=MUTED, bold=True)

    def node(x, y, w, h, head, sub, fill=PANEL, head_color=INK, sub_color=MUTED):
        card(s, x, y, w, h, fill=fill, name=head)
        text(s, x + Inches(0.2), y + Inches(0.15), w - Inches(0.4), Inches(0.45), head, size=16, bold=True,
             color=head_color)
        text(s, x + Inches(0.2), y + Inches(0.6), w - Inches(0.4), h - Inches(0.7), sub.split("\n"), size=12,
             color=sub_color, space_after=0)

    row1, nh = by + Inches(0.75), Inches(1.45)
    node(bx + Inches(0.3), row1, Inches(2.2), nh, "Browser", "you, signed in")
    node(bx + Inches(3.0), row1, Inches(2.6), nh, "Dashboard", "Streamlit\nno database password")
    node(bx + Inches(6.1), row1, Inches(2.9), nh, "API", "FastAPI · sign-in · roles\naudit · limits", fill=INK,
         head_color=WHITE, sub_color=ICE)
    node(bx + Inches(9.5), row1, Inches(2.33), nh, "Local LLM", "Ollama · qwen3:4b\nlocal GPU", fill=ACCENT,
         head_color=WHITE, sub_color=WHITE)
    row2 = by + Inches(3.0)
    node(bx + Inches(6.1), row2, Inches(2.9), Inches(1.9), "Agent workflow",
         "LangGraph · 6 agents\nchecks every LLM sentence\ncheckpoint after each step")
    node(bx + Inches(3.0), row2, Inches(2.6), Inches(1.9), "payments schema",
         "PostgreSQL 16\nread-only login\n(agents, analytics)")
    node(bx + Inches(9.5), row2, Inches(2.33), Inches(1.9), "ops schema",
         "PostgreSQL 16 · app login\nhistory, checkpoints\naccounts, audit log")
    mid = row1 + nh / 2
    arrow(s, bx + Inches(2.5), mid, bx + Inches(3.0), mid)
    arrow(s, bx + Inches(5.6), mid, bx + Inches(6.1), mid)
    arrow(s, bx + Inches(9.0), mid, bx + Inches(9.5), mid)
    arrow(s, bx + Inches(7.55), row1 + nh, bx + Inches(7.55), row2)
    arrow(s, bx + Inches(6.1), row2 + Inches(0.95), bx + Inches(5.6), row2 + Inches(0.95))
    arrow(s, bx + Inches(9.0), row2 + Inches(0.95), bx + Inches(9.5), row2 + Inches(0.95))
    text(s, bx + Inches(0.3), row2 + Inches(0.1), Inches(2.4), Inches(1.9),
         ["Not connected:", "cloud LLMs, API keys, telemetry, external identity providers"], size=13, color=MUTED)


def s_agents(prs):
    s = new_slide(prs, "Six agents, each with a narrow job", notes=(
        "Six agents, each with one job and a fixed list of tools it may call. The coordinator plans and assembles. "
        "Transaction intelligence compares this week with last. Pattern investigation finds anomaly windows and "
        "ranks hypotheses. Forecasting says what normal looks like next. Business impact sizes the damage with "
        "honest labels. Verification and policy trusts nothing: it recomputes figures from the database and checks "
        "every sentence. Every tool call is recorded, and every finding cites the calls behind it."))
    agents = [("Operations Coordinator", "plans, assembles, writes the report with the LLM", "dataset window, providers"),
              ("Transaction Intelligence", "this week vs last: rates, providers, reasons", "summary, breakdown"),
              ("Pattern Investigation", "anomaly windows, nearby events, ranked hypotheses", "time series, events"),
              ("Forecasting", "next week's volume and a normal failure rate", "time series"),
              ("Business Impact", "value at risk: measured, estimated, hypothetical", "summary"),
              ("Verification & Policy", "recomputes figures, checks every sentence", "summary, breakdown")]
    cw, ch, gx, gy = Inches(3.85), Inches(2.3), Inches(0.32), Inches(0.33)
    for i, (name, job, tools) in enumerate(agents):
        x = MARGIN + (cw + gx) * (i % 3)
        y = CONTENT_TOP + (ch + gy) * (i // 3)
        card(s, x, y, cw, ch)
        badge(s, x + Inches(0.25), y + Inches(0.25), str(i + 1), fill=ACCENT if i == 5 else INK, d=Inches(0.5))
        text(s, x + Inches(0.9), y + Inches(0.27), cw - Inches(1.1), Inches(0.5), name, size=16, bold=True)
        text(s, x + Inches(0.25), y + Inches(0.95), cw - Inches(0.5), Inches(0.8), job, size=14)
        text(s, x + Inches(0.25), y + Inches(1.75), cw - Inches(0.5), Inches(0.4), "tools: " + tools, size=12,
             color=MUTED)


def s_found(prs):
    s = new_slide(prs, "It found the hidden incident, and the timing", notes=(
        "Did it work? Yes. Across all providers, the weekly failure rate rose from 2.88 to 3.85 percent, a "
        "statistically significant change, and Kestrel explains 98 percent of the extra failures. Zooming in, "
        "Kestrel's failure rate went from 2.73 percent to 23.07 percent between 23 September 14:00 and 25 "
        "September 01:00. The injected incident ran from 14:00 on the 23rd to 02:00 on the 25th, so the window is "
        "right to the hour at the start and one hour short at the end. 87 percent of the failures in the window "
        "were timeouts, which is exactly what was injected. Source: docs/evaluation.md, seed 42."))
    cd = CategoryChartData()
    cd.categories = ["All providers, week", "KESTREL, anomaly window"]
    cd.add_series("Before", (2.88, 2.73))
    cd.add_series("During", (3.85, 23.07))
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, MARGIN, CONTENT_TOP, Inches(7.0), Inches(5.2), cd)
    ch = gf.chart
    style_chart(ch, decimals=2)
    color_series(ch, [LINE, ACCENT])
    ch.has_title = True
    ch.chart_title.text_frame.text = "Failure rate, before vs during"
    tr = ch.chart_title.text_frame.paragraphs[0].runs[0]
    _font(tr, BODY_FONT, 14, MUTED, bold=True)
    x = Inches(8.1)
    stat(s, x, CONTENT_TOP + Inches(0.1), Inches(4.6), "98%", "of the extra failures were at KESTREL", color=ACCENT)
    stat(s, x, CONTENT_TOP + Inches(1.65), Inches(4.6), "87%", "of failures in the window were TIMEOUT")
    stat(s, x, CONTENT_TOP + Inches(3.2), Inches(4.6), "0 h / 1 h", "window start / end vs the injected incident")
    text(s, x, Inches(6.55), Inches(4.6), Inches(0.5), "Source: docs/evaluation.md (seed 42)", size=11,
         color=MUTED)


def s_hypotheses(prs):
    s = new_slide(prs, "Hypotheses are ranked, never declared proven", notes=(
        "This is the part I am proudest of. A deployment went out two hours before the incident. A naive system, "
        "or a tired analyst, blames it. Our pattern agent checks: after that deployment, did every provider get "
        "worse, as you would expect if our own software broke? No, only Kestrel did, and our software serves all "
        "providers. So the deployment is ranked 'unlikely'. The provider-side problem leads, with a score of 6, "
        "but it is still called 'leading, unconfirmed', and the next step is to ask Kestrel for their incident "
        "report. The system shows its reasoning and does not overclaim."))
    cw, ch = Inches(5.9), Inches(4.6)
    for i, (rank, title, status, color, evidence) in enumerate([
        ("1", "Provider-side problem at KESTREL", "score 6 · leading (unconfirmed)", ACCENT,
         ["+ only KESTREL shows an anomaly", "+ 87% of window failures are TIMEOUT",
          "+ KESTREL posted a notice 3 h after the start", "+ after DEP-0026 only KESTREL got worse",
          "next: ask KESTREL for their incident report"]),
        ("2", "Our deployment DEP-0026", "score 0 · unlikely", INK,
         ["+ deployed 2 h before the anomaly", "− only KESTREL got worse; our software",
          "   serves all four providers", "timing overlap is not evidence of cause",
          "next: review the change and its logs"])]):
        x = MARGIN + (cw + Inches(0.33)) * i
        card(s, x, CONTENT_TOP, cw, ch)
        badge(s, x + Inches(0.3), CONTENT_TOP + Inches(0.3), rank, fill=color, d=Inches(0.6), size=18)
        text(s, x + Inches(1.1), CONTENT_TOP + Inches(0.28), cw - Inches(1.4), Inches(0.5), title, size=19, bold=True)
        text(s, x + Inches(1.1), CONTENT_TOP + Inches(0.8), cw - Inches(1.4), Inches(0.4), status, size=14,
             color=color, bold=True)
        text(s, x + Inches(0.4), CONTENT_TOP + Inches(1.55), cw - Inches(0.8), Inches(2.9), evidence, size=15,
             space_after=8)
    text(s, MARGIN, Inches(6.4), Inches(12), Inches(0.5),
         "Ground truth: KESTREL timeouts injected; DEP-0026 is a deliberate red herring. Both ranked correctly.",
         size=14, color=MUTED)


def s_verification(prs):
    s = new_slide(prs, "Every sentence the LLM writes is checked", notes=(
        "How do we trust text from a 4-billion-parameter model? We don't, we check it. Every sentence must cite "
        "a finding, every number must appear in the finding it cites, dates must be inside the period, and causal "
        "or proof language is removed unless hedged. Key figures are recomputed from the database. If more than "
        "30 percent of sentences are removed, the user gets the template report, clearly labelled. In the second "
        "evaluation run, 93.8 percent of sentences passed, and both prompt-injection attempts were handled safely."))
    checks = [("Cites a real finding", "every sentence names the IDs it is based on"),
              ("Numbers match", "each number appears in the finding it cites"),
              ("Dates are in scope", "only the investigated weeks or dates in findings"),
              ("No overclaiming", "'caused', 'proves', 'root cause' removed unless hedged"),
              ("Figures recomputed", "rates, window counts, value re-queried from the database")]
    for i, (head, sub) in enumerate(checks):
        y = CONTENT_TOP + Inches(0.95) * i
        badge(s, MARGIN, y, "✓", fill=GOOD, d=Inches(0.5))
        text(s, MARGIN + Inches(0.7), y - Inches(0.02), Inches(6.3), Inches(0.85),
             [[(head, {"bold": True, "size": 17})], [(sub, {"size": 14, "color": MUTED})]], space_after=0)
    card(s, Inches(8.0), CONTENT_TOP, Inches(4.73), Inches(4.65), fill=INK)
    stat(s, Inches(8.4), CONTENT_TOP + Inches(0.3), Inches(4.1), "93.8%", "of LLM sentences kept (run 2)",
         color=WHITE, label_color=ICE)
    stat(s, Inches(8.4), CONTENT_TOP + Inches(1.75), Inches(4.1), "2 / 2", "prompt-injection attempts handled safely",
         color=WHITE, label_color=ICE)
    stat(s, Inches(8.4), CONTENT_TOP + Inches(3.2), Inches(4.1), "> 30%", "removed → labelled template report",
         color=ACCENT, label_color=ICE)


def s_evaluation(prs):
    s = new_slide(prs, "Measured on ten questions with known answers", notes=(
        "We measured the real model on ten fixed questions: six ordinary, two prompt injections, two out of range. "
        "Run 1 found a bug in my own prompt, which I will come to on the next slide. After the fix, run 2: the plan "
        "was right for all ten questions, the verified LLM report was used every time, and 93.8 percent of "
        "sentences passed. Detection, measured separately on eight different random datasets, never raised a false "
        "alarm and caught 14 of 16 incidents; the two misses are the deliberately tiny ones below the reporting "
        "threshold. Ten questions is a small sample, so these numbers are indicative."))
    cd = CategoryChartData()
    cd.categories = ["Plan correct", "LLM report used", "Sentences kept"]
    cd.add_series("Run 1", (90.0, 90.0, 92.9))
    cd.add_series("Run 2 (after prompt fix)", (100.0, 100.0, 93.8))
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, MARGIN, CONTENT_TOP, Inches(7.4), Inches(5.2), cd)
    ch = gf.chart
    style_chart(ch)
    color_series(ch, [LINE, INK])
    ch.value_axis.maximum_scale = 110
    ch.value_axis.minimum_scale = 0
    ch.value_axis.major_unit = 25
    ch.has_title = True
    ch.chart_title.text_frame.text = "qwen3:4b on 10 questions (%)"
    _font(ch.chart_title.text_frame.paragraphs[0].runs[0], BODY_FONT, 14, MUTED, bold=True)
    x = Inches(8.5)
    text(s, x, CONTENT_TOP, Inches(4.2), Inches(0.4), "Anomaly detection, 8 random datasets", size=15, bold=True,
         color=MUTED)
    stat(s, x, CONTENT_TOP + Inches(0.5), Inches(2.0), "14/14", "alarms were real", color=GOOD)
    stat(s, x + Inches(2.1), CONTENT_TOP + Inches(0.5), Inches(2.1), "14/16", "incidents found")
    text(s, x, CONTENT_TOP + Inches(2.2), Inches(4.2), Inches(0.4), "Speed on the laptop", size=15, bold=True,
         color=MUTED)
    stat(s, x, CONTENT_TOP + Inches(2.7), Inches(2.0), "26 s", "median investigation")
    stat(s, x + Inches(2.1), CONTENT_TOP + Inches(2.7), Inches(2.1), "57 s", "slowest of 10 (p90)")
    text(s, x, Inches(6.55), Inches(4.2), Inches(0.5), "Source: docs/evaluation.md (run 2, Oct 2026)",
         size=11, color=MUTED)


def s_lessons(prs):
    s = new_slide(prs, "What the evaluation taught us", notes=(
        "Three honest lessons. First, a checker can be wrong too. The first time the real model ran, all four "
        "removed sentences were actually true; the checker misread a date as a number, among other things. Each "
        "case became a regression test. Second, my own prompt caused errors: it gave an example ID, and the model "
        "copied it when there were no hypotheses. Removing the example took invented IDs from four to zero. Third, "
        "what checking cannot catch: the model once copied a cost range correctly but attached it to the wrong "
        "time period. Numbers can be right while the meaning is wrong, which is why findings stay visible beside "
        "every sentence, and why human review of a sample is the next step."))
    lessons = [("The checker can be wrong too",
                "First real run: all 4 removed sentences were true (a date read as a number, a valid citation "
                "not recognised). Each became a regression test.", "4 → 0", "false removals"),
               ("My prompt caused errors",
                "An example ID in the instructions was copied when no hypotheses existed. Removing the example fixed it.",
                "4 → 0", "invented IDs"),
               ("What checks cannot catch",
                "A copied cost range was attached to the wrong period. Right numbers, wrong meaning: needs human review.",
                "open", "measured by sampling next")]
    cw, gap = Inches(3.85), Inches(0.32)
    for i, (head, body, big, small) in enumerate(lessons):
        x = MARGIN + (cw + gap) * i
        card(s, x, CONTENT_TOP, cw, Inches(5.0))
        badge(s, x + Inches(0.3), CONTENT_TOP + Inches(0.3), str(i + 1), fill=ACCENT if i == 2 else INK)
        text(s, x + Inches(0.3), CONTENT_TOP + Inches(1.05), cw - Inches(0.6), Inches(0.8), head, size=18, bold=True)
        text(s, x + Inches(0.3), CONTENT_TOP + Inches(1.85), cw - Inches(0.6), Inches(1.7), body, size=14)
        text(s, x + Inches(0.3), CONTENT_TOP + Inches(3.6), cw - Inches(0.6), Inches(0.7), big, size=34, bold=True,
             color=ACCENT if i == 2 else INK, face=HEAD_FONT)
        text(s, x + Inches(0.3), CONTENT_TOP + Inches(4.3), cw - Inches(0.6), Inches(0.4), small, size=13, color=MUTED)


def s_resilience(prs):
    s = new_slide(prs, "Runs survive interruptions and stay bounded", notes=(
        "Resilience. After every step, its output is saved as a checkpoint. When I stopped a run with Control-C "
        "while the model was writing, the resume reused the six finished steps and only re-ran the report, "
        "verification and finalize; it took 15.7 seconds instead of starting over. Checkpoints are plain JSON "
        "validated against the workflow's types, never unpickled objects, which avoids a class of vulnerability "
        "LangGraph's own checkpoint loader had. Every run is bounded by limits on time, steps, tool calls, LLM "
        "calls and concurrent runs."))
    steps = ["plan", "transaction", "pattern", "forecast", "impact", "assemble", "narrate", "verify", "finalize"]
    n, gap = len(steps), Inches(0.12)
    sw = (W - 2 * MARGIN - gap * (n - 1)) / n
    y = CONTENT_TOP + Inches(0.3)
    for i, name in enumerate(steps):
        x = MARGIN + (sw + gap) * i
        reused = i < 6
        badge(s, x + (sw - Inches(0.6)) / 2, y, "↺" if reused else "▶", fill=INK if reused else ACCENT,
              d=Inches(0.6), size=18)
        text(s, x, y + Inches(0.72), sw, Inches(0.4), name, size=13, align=PP_ALIGN.CENTER)
    text(s, MARGIN, y + Inches(1.25), Inches(12), Inches(0.5),
         [[("↺ reused from checkpoint", {"bold": True, "color": INK}), ("     ", {}),
           ("▶ run again after the interruption", {"bold": True, "color": ACCENT}),
           ("     resumed in 15.7 s, on your laptop", {"color": MUTED})]], size=14)
    rows = [["Limit", "Default", "When reached"],
            ["Time per investigation", "600 s", "labelled partial report, resumable"],
            ["Steps per run", "20 (9 needed)", "stopped, recorded as interrupted"],
            ["Tool calls per agent step", "30 (busiest uses 9)", "that step fails clearly"],
            ["LLM calls per investigation", "4", "template report"],
            ["Investigations at once", "2", "HTTP 429 'try again'"]]
    table(s, MARGIN, Inches(3.75), Inches(12.1), rows, [Inches(4.0), Inches(3.3), Inches(4.8)], size=14,
          row_h=Inches(0.47))


def s_security(prs):
    s = new_slide(prs, "Access is enforced by the backend, and recorded", notes=(
        "Security. Local accounts, passwords hashed with scrypt at OWASP's parameters, sessions held on the server "
        "so logout and disabling take effect immediately, and a lockout after five wrong passwords. Four roles. "
        "Note the separation of duties: the admin manages accounts and reads the audit log but cannot run "
        "investigations. Someone else's investigation answers 'not found', so IDs cannot be probed. Every sign-in, "
        "investigation, account change and refusal goes into an append-only audit log; a database trigger blocks "
        "changes even by the owner, and a hash chain shows exactly which row was altered."))
    Y, N = "✓", "–"
    rows = [["Permission", "viewer", "analyst", "lead", "admin"],
            ["See dashboards and KPIs", Y, Y, Y, Y],
            ["Run investigations", N, Y, Y, N],
            ["See and resume own", N, Y, Y, N],
            ["See everyone's", N, N, Y, Y],
            ["Resume everyone's", N, N, Y, N],
            ["Manage accounts, read audit log", N, N, N, Y]]
    table(s, MARGIN, CONTENT_TOP, Inches(7.4), rows, [Inches(3.4), Inches(1.0), Inches(1.0), Inches(1.0), Inches(1.0)],
          size=14, row_h=Inches(0.55))
    x = Inches(8.4)
    for i, (head, sub) in enumerate([("scrypt, OWASP parameters", "128 MiB per hash; lockout after 5 tries"),
                                     ("Server-side sessions", "30 min idle, 8 h maximum; logout is immediate"),
                                     ("Append-only audit log", "trigger blocks edits; hash chain finds the row"),
                                     ("Least privilege, 3 logins", "agents read-only; API holds no owner password")]):
        y = CONTENT_TOP + Inches(1.0) * i
        badge(s, x, y, str(i + 1), fill=ACCENT if i == 2 else INK, d=Inches(0.5))
        text(s, x + Inches(0.7), y - Inches(0.02), Inches(3.7), Inches(0.9),
             [[(head, {"bold": True, "size": 16})], [(sub, {"size": 13, "color": MUTED})]], space_after=0)
    text(s, MARGIN, Inches(6.55), Inches(12), Inches(0.4),
         "A test fails if any API route other than /health and sign-in is left unprotected.", size=13, color=MUTED)


def s_engineering(prs):
    s = new_slide(prs, "Built and tested like a product", notes=(
        "Engineering. Nine phases, each finished only when its tests passed on my laptop: from 37 tests in phase 1 "
        "to 435 in phase 8, including tests against the real database and the real local model. The whole system "
        "also runs as a Docker image: non-root, ports on 127.0.0.1 only, and no container holds a password it "
        "doesn't need. A GitHub Actions workflow is written to run the tests, build the image end to end and audit "
        "dependencies on every push; it has not been run on GitHub yet, and I say that plainly."))
    cd = CategoryChartData()
    cd.categories = ["P1", "P2", "P3", "P4", "P5a", "P5b", "P6", "P7", "P8"]
    cd.add_series("Tests passing", (37, 69, 126, 172, 206, 267, 323, 411, 435))
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, MARGIN, CONTENT_TOP, Inches(7.4), Inches(5.2), cd)
    ch = gf.chart
    style_chart(ch, percent=False, legend=False)
    color_series(ch, [INK])
    ch.plots[0].gap_width = 60
    ch.has_title = True
    ch.chart_title.text_frame.text = "Tests passing on the development laptop, by phase"
    _font(ch.chart_title.text_frame.paragraphs[0].runs[0], BODY_FONT, 14, MUTED, bold=True)
    x = Inches(8.5)
    for i, (head, sub, mark, fill) in enumerate([
            ("Docker image", "built and running: db, API, dashboard healthy", "✓", GOOD),
            ("Non-root, 127.0.0.1", "uid 10001; nothing reachable from the network", "✓", GOOD),
            ("Real services in tests", "PostgreSQL and the local LLM, not only mocks", "✓", GOOD),
            ("GitHub Actions: written", "tests, Docker end to end, dependency audit; not yet run on GitHub", "!",
             ACCENT)]):
        y = CONTENT_TOP + Inches(1.15) * i
        badge(s, x, y, mark, fill=fill, d=Inches(0.5))
        text(s, x + Inches(0.7), y - Inches(0.02), Inches(3.6), Inches(1.0),
             [[(head, {"bold": True, "size": 16})], [(sub, {"size": 13, "color": MUTED})]], space_after=0)


def s_limits(prs):
    s = new_slide(prs, "Honest limits and what comes next", notes=(
        "Honest limits. The data is synthetic and cleaner than real data, so real-world accuracy would be lower. "
        "The model evaluation is ten questions, indicative not precise. Right-numbers-wrong-meaning errors need "
        "human review to measure. If you ask about a provider that doesn't exist, the report quietly reviews all "
        "providers instead of saying so. Next: a larger model on my new PC with an RTX 5070 Ti, using the same "
        "evaluation so results compare directly; a human review sample; per-provider data scoping for analysts; "
        "and running CI on GitHub."))
    cols = [("Limits today", INK, ["Synthetic data: cleaner incidents than real life",
                                   "LLM evaluated on 10 questions: indicative only",
                                   "Right numbers, wrong meaning: not caught by checks",
                                   "Unknown provider names are not flagged to the user",
                                   "CI written but not yet run on GitHub"]),
            ("Next steps", ACCENT, ["qwen3:8b / 14b on the RTX 5070 Ti, same evaluation",
                                    "Human review of a sample of reports",
                                    "Per-provider data scoping for analyst accounts",
                                    "Flag names in questions that match no provider",
                                    "Push to GitHub and get the three CI jobs green"])]
    cw = Inches(5.9)
    for i, (head, color, items) in enumerate(cols):
        x = MARGIN + (cw + Inches(0.33)) * i
        card(s, x, CONTENT_TOP, cw, Inches(4.1))
        badge(s, x + Inches(0.3), CONTENT_TOP + Inches(0.3), "!" if i == 0 else "→", fill=color)
        text(s, x + Inches(1.05), CONTENT_TOP + Inches(0.35), cw - Inches(1.3), Inches(0.5), head, size=20, bold=True)
        bullets(s, x + Inches(0.4), CONTENT_TOP + Inches(1.2), cw - Inches(0.8), Inches(3.7), items, size=16)


def s_demo(prs):
    s = new_slide(prs, "Demo", dark=True, notes=(
        "Now the live demo. I sign in as an analyst, ask what went wrong with Kestrel in the week of 21 September, "
        "and we watch the agents work. Then I show the verification panel and the evidence behind each sentence. "
        "Then I sign in as the admin to show that the Investigate tab is gone for that role, and verify the audit "
        "log's hash chain. If the live model is slow, the fallback is the CLI with --no-llm, which answers in under "
        "a second with the labelled template report. The full script is in docs/demo_script.md."))
    s.shapes.title.top = Inches(1.0)
    steps = [("Sign in as an analyst", "ana (README) · Investigate tab"),
             ("Ask about Kestrel, week of 21 Sep", "watch six agents work, ~20–30 s"),
             ("Open verification and evidence", "every sentence cites its findings"),
             ("Sign in as admin", "no Investigate tab · verify the audit chain")]
    for i, (head, sub) in enumerate(steps):
        y = Inches(2.3) + Inches(1.05) * i
        badge(s, MARGIN, y, str(i + 1), fill=ACCENT if i == 1 else INK_SOFT, d=Inches(0.6), size=18)
        text(s, MARGIN + Inches(0.85), y - Inches(0.02), Inches(7.5), Inches(0.9),
             [[(head, {"bold": True, "size": 20, "color": WHITE})], [(sub, {"size": 15, "color": ICE})]], space_after=0)
    card(s, Inches(8.3), Inches(2.3), Inches(4.43), Inches(3.0), fill=INK_SOFT)
    text(s, Inches(8.6), Inches(2.55), Inches(3.9), Inches(2.6),
         [[("Run it yourself", {"bold": True, "size": 17, "color": WHITE})],
          [("docker compose --profile app up -d", {"face": "Courier New", "size": 12, "color": ICE})],
          [("http://127.0.0.1:8501", {"face": "Courier New", "size": 12, "color": ICE})],
          [("Setup: README.md", {"size": 14, "color": ICE})],
          [("docs/demo_script.md", {"size": 14, "color": ICE})]], space_after=10)


SLIDES = [s_title, s_problem, s_flow, s_principles, s_architecture, s_agents, s_found, s_hypotheses,
          s_verification, s_evaluation, s_lessons, s_resilience, s_security, s_engineering, s_limits, s_demo]


def build(deck_path: Path = OUT_DECK, notes_path: Path | None = OUT_NOTES) -> Path:
    NOTES.clear()
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    for make in SLIDES:
        make(prs)
    cp = prs.core_properties
    cp.title = "Payment Operations Intelligence"
    cp.subject = "A local, multi-agent AI system for investigating payment failures"
    cp.author = "Payment Operations Intelligence project"
    deck_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(deck_path)
    if notes_path is not None:
        lines = ["# Presentation notes", "",
                 "Speaker notes for `presentation/payment_ops_intelligence.pptx`, generated by "
                 "`scripts/build_presentation.py` from the same text as the slides' notes. About 20–25 minutes "
                 f"for {len(NOTES)} slides, plus questions. Edit the script, not this file.", ""]
        for i, n in enumerate(NOTES, 1):
            lines += [f"## {i}. {n.title}", "", n.text, ""]
        notes_path.write_text("\n".join(lines), encoding="utf-8")
    return deck_path


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path.relative_to(ROOT)} ({len(NOTES)} slides) and {OUT_NOTES.relative_to(ROOT)}")
    sys.exit(0)
