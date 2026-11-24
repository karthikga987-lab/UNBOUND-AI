"""
Generate the official 8-slide presentation for wHACKiest 2026 Track 2.
Team: Khabi Kushi Khabi Bug
College: MS Ramaiah Institute of Technology
Track: Track 2 - Beyond the Browser: Reimagine How We Use the Web
"""
import sys
from pathlib import Path
import pptx
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

TEMPLATE_PATH = Path(r"C:\Users\LENOVO\.gemini\antigravity\brain\675a0efb-f980-4f25-baad-84c98fc798db\.user_uploaded\media_1795522655912_dec4b4f6.pptx")
OUTPUT_NAME = "Khabi Kushi Khabi Bug_Track2.pptx"
OUTPUT_PATH_1 = Path(r"F:\whackiest") / OUTPUT_NAME
OUTPUT_PATH_2 = Path(r"F:\whackiest\UNBOUND-AI") / OUTPUT_NAME

# Brand colors matching the template & Ramaiah palette
PRIMARY_NAVY = RGBColor(17, 24, 39)      # #111827 Dark navy/black
ACCENT_PURPLE = RGBColor(91, 33, 182)    # #5B21B6 Deep purple (matching Ramaiah logo)
DARK_GRAY = RGBColor(55, 65, 81)         # #374151 Body text gray
MUTED_GRAY = RGBColor(107, 114, 128)     # #6B7280 Muted gray
BG_CARD = RGBColor(248, 250, 252)        # #F8FAFC Card background
BORDER_GRAY = RGBColor(226, 232, 240)    # #E2E8F0 Border gray
WHITE = RGBColor(255, 255, 255)
AMBER_BG = RGBColor(254, 243, 199)
AMBER_BORDER = RGBColor(217, 119, 6)
AMBER_TEXT = RGBColor(180, 83, 9)


def add_slide_footer(slide):
    """Add bottom-left footer on slides 2-8: Team Name and empty Team ID for future editing."""
    ft_box = slide.shapes.add_textbox(Inches(0.69), Inches(6.92), Inches(8.5), Inches(0.32))
    tf = ft_box.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0)
    tf.margin_top = Inches(0)
    tf.margin_bottom = Inches(0)
    tf.margin_right = Inches(0)
    p = tf.paragraphs[0]

    r1 = p.add_run()
    r1.text = "wHACKiest 2026  ·  Team Name: "
    r1.font.name = "Calibri"
    r1.font.size = Pt(10.5)
    r1.font.color.rgb = MUTED_GRAY

    r2 = p.add_run()
    r2.text = "Khabi Kushi Khabi Bug"
    r2.font.name = "Calibri"
    r2.font.size = Pt(10.5)
    r2.font.bold = True
    r2.font.color.rgb = PRIMARY_NAVY

    r3 = p.add_run()
    r3.text = "  ·  Team ID: "
    r3.font.name = "Calibri"
    r3.font.size = Pt(10.5)
    r3.font.bold = True
    r3.font.color.rgb = MUTED_GRAY


def setup_slide_1(slide):
    """Fill in team and member info on Slide 1."""
    for s in slide.shapes:
        if s.shape_id == 32:
            s.text_frame.clear()
            p = s.text_frame.paragraphs[0]
            r1 = p.add_run()
            r1.text = "TEAM NAME:  "
            r1.font.name = "Calibri"
            r1.font.size = Pt(15)
            r1.font.bold = True
            r1.font.color.rgb = ACCENT_PURPLE

            r2 = p.add_run()
            r2.text = "Khabi Kushi Khabi Bug"
            r2.font.name = "Calibri"
            r2.font.size = Pt(16)
            r2.font.bold = True
            r2.font.color.rgb = PRIMARY_NAVY

        elif s.shape_id == 34:
            s.text_frame.clear()
            p = s.text_frame.paragraphs[0]
            r1 = p.add_run()
            r1.text = "COLLEGE NAME:  "
            r1.font.name = "Calibri"
            r1.font.size = Pt(15)
            r1.font.bold = True
            r1.font.color.rgb = ACCENT_PURPLE

            r2 = p.add_run()
            r2.text = "MS Ramaiah Institute of Technology"
            r2.font.name = "Calibri"
            r2.font.size = Pt(15)
            r2.font.bold = True
            r2.font.color.rgb = PRIMARY_NAVY

        elif s.shape_id == 36:
            s.text_frame.clear()
            p = s.text_frame.paragraphs[0]
            r1 = p.add_run()
            r1.text = "TRACK:  "
            r1.font.name = "Calibri"
            r1.font.size = Pt(15)
            r1.font.bold = True
            r1.font.color.rgb = ACCENT_PURPLE

            r2 = p.add_run()
            r2.text = "Track 2 — Beyond the Browser: Reimagine How We Use the Web"
            r2.font.name = "Calibri"
            r2.font.size = Pt(14)
            r2.font.bold = True
            r2.font.color.rgb = PRIMARY_NAVY

        elif s.shape_id == 38:
            s.text_frame.clear()
            p = s.text_frame.paragraphs[0]
            r1 = p.add_run()
            r1.text = "TEAM ID:  "
            r1.font.name = "Calibri"
            r1.font.size = Pt(15)
            r1.font.bold = True
            r1.font.color.rgb = ACCENT_PURPLE

        elif s.shape_id == 40:
            s.text_frame.clear()
            members = [
                "Member 1 (Lead): Karthik GA",
                "Member 2: Mussavir MB",
                "Member 3: Nandakishore AK",
                "Member 4: Hussain MJ",
            ]
            for i, m in enumerate(members):
                p = s.text_frame.paragraphs[0] if i == 0 else s.text_frame.add_paragraph()
                r = p.add_run()
                r.text = m
                r.font.name = "Calibri"
                r.font.size = Pt(13)
                r.font.bold = True
                r.font.color.rgb = PRIMARY_NAVY

        elif s.shape_id == 41:
            s.text_frame.clear()
            usns = [
                "USN: 1MS25CS076",
                "USN: 1MS25CS108",
                "USN: 1MS25CS114",
                "USN: 1MS25CS072",
            ]
            for i, u in enumerate(usns):
                p = s.text_frame.paragraphs[0] if i == 0 else s.text_frame.add_paragraph()
                r = p.add_run()
                r.text = u
                r.font.name = "Calibri"
                r.font.size = Pt(13)
                r.font.bold = True
                r.font.color.rgb = ACCENT_PURPLE

    # No footer on Slide 1 to prevent overlapping with the team/member cards


def clear_placeholder_shapes(slide):
    """Clear text in Shape 2 ('INCLUDE:') and Shape 3."""
    if len(slide.shapes) > 2 and slide.shapes[2].has_text_frame:
        slide.shapes[2].text_frame.clear()
    if len(slide.shapes) > 3 and slide.shapes[3].has_text_frame:
        slide.shapes[3].text_frame.clear()


def create_header_card(slide, title, subtitle):
    """Set title and subtitle."""
    if len(slide.shapes) > 0 and slide.shapes[0].has_text_frame:
        slide.shapes[0].text_frame.paragraphs[0].text = title
        slide.shapes[0].text_frame.paragraphs[0].font.bold = True
        slide.shapes[0].text_frame.paragraphs[0].font.color.rgb = PRIMARY_NAVY
    if len(slide.shapes) > 1 and slide.shapes[1].has_text_frame:
        slide.shapes[1].text_frame.paragraphs[0].text = subtitle
        slide.shapes[1].text_frame.paragraphs[0].font.color.rgb = MUTED_GRAY


def add_bullet_point(tf, header, body, is_first=False, header_color=ACCENT_PURPLE, font_size=11.5):
    """Add a clean structured bullet point with bold title."""
    p = tf.paragraphs[0] if is_first else tf.add_paragraph()
    p.space_after = Pt(5)
    p.space_before = Pt(2)
    
    r_bullet = p.add_run()
    r_bullet.text = "▪ "
    r_bullet.font.name = "Calibri"
    r_bullet.font.size = Pt(font_size)
    r_bullet.font.bold = True
    r_bullet.font.color.rgb = header_color

    r_hdr = p.add_run()
    r_hdr.text = header + ": "
    r_hdr.font.name = "Calibri"
    r_hdr.font.size = Pt(font_size)
    r_hdr.font.bold = True
    r_hdr.font.color.rgb = header_color

    r_body = p.add_run()
    r_body.text = body
    r_body.font.name = "Calibri"
    r_body.font.size = Pt(font_size)
    r_body.font.color.rgb = DARK_GRAY


def setup_slide_2(slide):
    """Slide 2: Problem Statement."""
    create_header_card(slide, "Problem Statement", "The Friction of the Modern Web: Fragile Automation, Read-Only Chatbots & Black-Box Agents")
    clear_placeholder_shapes(slide)

    tb = slide.shapes.add_textbox(Inches(0.7), Inches(2.35), Inches(11.9), Inches(4.35))
    tf = tb.text_frame
    tf.word_wrap = True

    add_bullet_point(
        tf,
        "The Structural Problem",
        "The web was engineered as a visual document rendering system for human eyes, not an execution layer for software. Consequently, knowledge workers spend hours manually switching tabs, keying data across isolated portals, extracting tables from unstructured web pages, and reconciling browser content with local documents.",
        is_first=True,
        font_size=11.5,
    )

    add_bullet_point(
        tf,
        "Who It Affects & Workflow Friction",
        "Analysts, researchers, compliance teams, procurement specialists, and motor-impaired users who must execute multi-step web routines across disparate portals. Because web platforms lack unified APIs, cross-system workflows require continuous manual intervention, repetitive navigation, and error-prone copy-pasting.",
        font_size=11.5,
    )

    add_bullet_point(
        tf,
        "Limitations of Existing Approaches",
        "• Traditional RPA (Selenium/Puppeteer scripts): Relies on rigid, hardcoded CSS/XPath selectors that break when class names, responsive layouts, or shadow DOM trees mutate.\n"
        "• Conversational AI & AI Search (ChatGPT, Perplexity): Trapped behind a read-only boundary; can summarize static text but cannot log into portals, trigger actions, or execute live browser transactions.\n"
        "• Emerging Autonomous Web Agents: Unconstrained black-box execution loops with high failure rates, no verifiable evidence provenance, and a critical lack of human approval gates for sensitive actions.",
        font_size=11,
    )

    add_bullet_point(
        tf,
        "The Missing Architectural Paradigm",
        "An intelligent, domain-neutral agent that bridges the gap: combining programming-by-demonstration to learn reusable browser workflows, self-healing multi-locator execution, native document context via MCP, and cryptographic human-in-the-loop safety gates.",
        font_size=11.5,
    )

    add_slide_footer(slide)


def setup_slide_3(slide):
    """Slide 3: Proposed Solution."""
    create_header_card(slide, "Proposed Solution: UNBOUND AI", "An Autonomous, Local-First Web Agent with Verifiable Evidence & Human Safety Controls")
    clear_placeholder_shapes(slide)

    tb = slide.shapes.add_textbox(Inches(0.7), Inches(2.35), Inches(11.9), Inches(4.35))
    tf = tb.text_frame
    tf.word_wrap = True

    add_bullet_point(
        tf,
        "Core Solution Thesis",
        "UNBOUND AI transforms the web browser from a manually operated visual viewport into an intelligent, user-directed execution environment. It executes natural-language goals across web interfaces and local documents with strict verification, observable state transitions, and zero hallucinations.",
        is_first=True,
        font_size=11.5,
    )

    add_bullet_point(
        tf,
        "Key Architectural Capabilities",
        "• Workflow Learning by Demonstration (PBD): Users perform a browser routine once; UNBOUND AI captures the sequence, parameterizes dynamic inputs (dates, queries, SKUs), and stores a permanent, replayable automation template.\n"
        "• Autonomous Browser Intelligence: Playwright Chromium async engine operating under an Observe → Act → Verify paradigm, utilizing a multi-locator fallback ensemble (ARIA roles > visible text > DOM hierarchy > coordinates).\n"
        "• Model Context Protocol (MCP) Integration: Standardized stdio document service connecting local PDF, DOCX, and TXT files with SQLite WAL persistence and sub-50ms TF-IDF chunk retrieval.\n"
        "• Human-in-the-Loop Safety Gate: Mandatory explicit approval interrupts for sensitive, financial, or state-altering actions (payments, data deletion, submissions), cryptographically bound to exact parameters.",
        font_size=11,
    )

    add_bullet_point(
        tf,
        "End-to-End User Experience",
        "User issues a high-level goal → Local Ollama model generates a structured tool plan → Agent cross-references local documents via MCP and executes browser steps → Pauses for human confirmation on sensitive actions → Verifies DOM state mutations → Delivers an evidence-cited answer.",
        font_size=11.5,
    )

    add_bullet_point(
        tf,
        "Local-First & Privacy Preserving",
        "Designed to run with local Ollama inference models (Qwen 3.5 4B, Qwen 3 8B, Llama 3.2 3B), keeping proprietary documents, enterprise session cookies, and DOM data strictly within the user's controlled infrastructure.",
        font_size=11.5,
    )

    add_slide_footer(slide)


def setup_slide_4(slide):
    """Slide 4: Tech Stack."""
    create_header_card(slide, "System Technology Stack", "Modular, Protocol-Standard Architecture Built with Strict Data Contracts")
    clear_placeholder_shapes(slide)

    cards_data = [
        ("User & Presentation Layer", "React 18 · Vite · Tailwind CSS · SSE", [
            "Real-time task execution timeline via Server-Sent Events",
            "Live browser session view & workflow recording controls",
            "Cryptographic Human-in-the-Loop approval modal",
            "Traceable evidence drawer with side-by-side citations",
        ]),
        ("Core Backend & Orchestration", "Python 3.12 · FastAPI · Pydantic 2", [
            "Strict shared schema contracts (Task, Step, ToolResult)",
            "Deterministic TaskManager finite-state machine",
            "TaskExecutor with atomic lock & step recovery loops",
            "Independent Outcome Verifier evaluating DOM mutations",
        ]),
        ("Browser Intelligence Subsystem", "Playwright · Chromium · Async Engine", [
            "Observe → Act → Verify execution loop with auto-wait",
            "Multi-locator ensemble: ARIA role > text > DOM > coords",
            "Workflow demonstration recorder with dynamic tokenization",
            "Sandboxed browser profiles, cookie management & live feed",
        ]),
        ("Intelligence, MCP & Storage", "Ollama · MCP SDK · SQLite WAL · S3", [
            "Local LLM inference: Qwen 3.5 4B (default), Qwen 3 8B, Llama 3.2",
            "Official Python MCP SDK exposing stdio tools",
            "PyMuPDF + python-docx safe parsing & TF-IDF search",
            "SQLite WAL metadata + AWS S3 staging (ap-south-1 verified)",
        ]),
    ]

    card_w = Inches(2.8)
    card_h = Inches(3.3)
    card_gap = Inches(0.23)
    start_x = Inches(0.7)
    start_y = Inches(2.35)

    for i, (title, sub, bullets) in enumerate(cards_data):
        x = start_x + i * (card_w + card_gap)
        
        rect = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, start_y, card_w, card_h)
        rect.fill.solid()
        rect.fill.fore_color.rgb = BG_CARD
        rect.line.color.rgb = ACCENT_PURPLE if (i == 2 or i == 3) else BORDER_GRAY
        rect.line.width = Pt(1.5 if (i == 2 or i == 3) else 1)

        tf = rect.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.14)
        tf.margin_right = Inches(0.14)
        tf.margin_top = Inches(0.14)
        tf.margin_bottom = Inches(0.14)

        p0 = tf.paragraphs[0]
        p0.text = title
        p0.font.name = "Calibri"
        p0.font.size = Pt(13)
        p0.font.bold = True
        p0.font.color.rgb = ACCENT_PURPLE

        p1 = tf.add_paragraph()
        p1.text = sub
        p1.font.name = "Calibri"
        p1.font.size = Pt(9.5)
        p1.font.italic = True
        p1.font.color.rgb = MUTED_GRAY
        p1.space_after = Pt(6)

        for b in bullets:
            pb = tf.add_paragraph()
            pb.text = "• " + b
            pb.font.name = "Calibri"
            pb.font.size = Pt(10)
            pb.font.color.rgb = DARK_GRAY
            pb.space_after = Pt(3.5)

    ft = slide.shapes.add_textbox(Inches(0.7), Inches(5.82), Inches(11.9), Inches(0.85))
    ft_tf = ft.text_frame
    ft_tf.word_wrap = True
    p_ft = ft_tf.paragraphs[0]
    p_ft.text = "Architectural Justification: Pydantic 2 eliminates cross-subsystem runtime schema drift; Playwright provides native event-driven auto-waiting over brittle Selenium sleeps; Official MCP SDK ensures universal agent tool composability; SQLite WAL + TF-IDF provides deterministic sub-50ms local context retrieval without heavy cloud vector infrastructure."
    p_ft.font.name = "Calibri"
    p_ft.font.size = Pt(10.5)
    p_ft.font.bold = True
    p_ft.font.color.rgb = ACCENT_PURPLE

    add_slide_footer(slide)


def setup_slide_5(slide):
    """Slide 5: Process Flow & System Architecture (Clean, NO white background box)."""
    create_header_card(slide, "Process Flow & System Architecture", "Step-by-Step Execution Loop from User Intent to Verified Evidence")
    clear_placeholder_shapes(slide)

    # Top: Process Flow (5 linear steps)
    steps = [
        ("1. Intent & Input", "Natural Language Prompt\nDoc Upload / Replay Trigger"),
        ("2. Plan & Decompose", "Local LLM Planning\nSchema-Validated Tool Call"),
        ("3. Safety Gate", "Permission Classification\nCryptographic User Approval"),
        ("4. Observe & Act", "Playwright Browser Action\nMCP Document Retrieval"),
        ("5. Verify & Synthesize", "DOM Mutation Assertion\nEvidenceItem Traceability"),
    ]

    sw = Inches(2.15)
    sh = Inches(1.05)
    sgap = Inches(0.28)
    sx = Inches(0.7)
    sy = Inches(2.35)

    for i, (title, desc) in enumerate(steps):
        x = sx + i * (sw + sgap)
        box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, sy, sw, sh)
        box.fill.solid()
        box.fill.fore_color.rgb = BG_CARD if i != 2 else AMBER_BG
        box.line.color.rgb = ACCENT_PURPLE if i != 2 else AMBER_BORDER
        box.line.width = Pt(1.5)

        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_top = Inches(0.08)
        p = tf.paragraphs[0]
        p.text = title
        p.font.name = "Calibri"
        p.font.size = Pt(11)
        p.font.bold = True
        p.font.color.rgb = ACCENT_PURPLE if i != 2 else AMBER_TEXT
        p.alignment = PP_ALIGN.CENTER

        p2 = tf.add_paragraph()
        p2.text = desc
        p2.font.name = "Calibri"
        p2.font.size = Pt(9.5)
        p2.font.color.rgb = DARK_GRAY
        p2.alignment = PP_ALIGN.CENTER

        if i < len(steps) - 1:
            arrow = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, x + sw + Inches(0.05), sy + Inches(0.38), Inches(0.18), Inches(0.25))
            arrow.fill.solid()
            arrow.fill.fore_color.rgb = ACCENT_PURPLE
            arrow.line.fill.background()

    # Section divider label placed directly on background
    lbl_box = slide.shapes.add_textbox(Inches(0.7), Inches(3.55), Inches(11.9), Inches(0.3))
    lbl_tf = lbl_box.text_frame
    lbl_tf.margin_left = Inches(0)
    lbl_tf.margin_top = Inches(0)
    p_lbl = lbl_tf.paragraphs[0]
    p_lbl.text = "CORE ARCHITECTURAL SUBSYSTEMS & COMPONENT ROLES"
    p_lbl.font.name = "Calibri"
    p_lbl.font.size = Pt(11)
    p_lbl.font.bold = True
    p_lbl.font.color.rgb = ACCENT_PURPLE

    # 3 architectural columns placed directly on the slide background
    arch_cols = [
        ("User Interaction & API Gateway", [
            "• React 18 / Tailwind Dashboard with live SSE execution stream",
            "• FastAPI Gateway enforcing strict Pydantic 2 data contracts",
            "• TaskManager: Finite state machine (Created → Running → Completed)",
            "• Approval Engine: Cryptographic ApprovalRequest binding token",
        ]),
        ("Orchestration & Planning Core", [
            "• Intent Analyzer: Classifies query vs executable multi-step task",
            "• Tool Registry: Strict schema validation & sandboxed boundaries",
            "• TaskExecutor: Atomic execution lock, retry policies & error recovery",
            "• Outcome Verifier: Independent DOM mutation assertions",
        ]),
        ("Intelligence & Tool Providers", [
            "• Playwright Engine: Multi-locator fallback & workflow recorder",
            "• MCP Stdio Server: Document chunking, search & entity linking",
            "• Storage Subsystem: SQLite WAL metadata + flat text file storage",
            "• Cloud Staging: AWS S3 bucket integration (ap-south-1 verified)",
        ]),
    ]

    cw = Inches(3.8)
    ch = Inches(2.65)
    cy = Inches(3.90)
    c_gap = Inches(0.25)
    
    for j, (col_title, items) in enumerate(arch_cols):
        cx = Inches(0.7) + j * (cw + c_gap)
        cbox = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, cx, cy, cw, ch)
        cbox.fill.solid()
        cbox.fill.fore_color.rgb = BG_CARD
        cbox.line.color.rgb = ACCENT_PURPLE if j == 1 else BORDER_GRAY
        cbox.line.width = Pt(1.5 if j == 1 else 1.2)

        ctf = cbox.text_frame
        ctf.word_wrap = True
        ctf.margin_left = Inches(0.18)
        ctf.margin_right = Inches(0.18)
        ctf.margin_top = Inches(0.15)

        cp = ctf.paragraphs[0]
        cp.text = col_title
        cp.font.name = "Calibri"
        cp.font.size = Pt(12)
        cp.font.bold = True
        cp.font.color.rgb = ACCENT_PURPLE
        cp.space_after = Pt(6)

        for item in items:
            cip = ctf.add_paragraph()
            cip.text = item
            cip.font.name = "Calibri"
            cip.font.size = Pt(10)
            cip.font.color.rgb = DARK_GRAY
            cip.space_after = Pt(3)

    add_slide_footer(slide)


def setup_slide_6(slide):
    """Slide 6: Technical Novelty & Competitive Differentiation."""
    create_header_card(slide, "Technical Novelty & Differentiation", "Bridging the Gap Between Brittle RPA, Static Chatbots, and Unchecked Web Agents")
    clear_placeholder_shapes(slide)

    tb = slide.shapes.add_textbox(Inches(0.7), Inches(2.35), Inches(11.9), Inches(1.15))
    tf = tb.text_frame
    tf.word_wrap = True

    add_bullet_point(
        tf,
        "Architectural Differentiator",
        "UNBOUND AI is the first domain-neutral web execution agent to unify Programming-by-Demonstration (workflow learning) with self-healing multi-locator execution, independent DOM outcome verification, and Model Context Protocol (MCP) evidence provenance.",
        is_first=True,
        font_size=11,
    )

    add_bullet_point(
        tf,
        "Core Technical Innovations",
        "• Self-Healing Locator Ensemble: Gracefully cascades across ARIA accessibility roles, visible text, DOM hierarchy, and relative coordinates to withstand layout drift.\n"
        "• Decoupled Outcome Verification: Verifies state changes via independent DOM assertions rather than relying on tool return codes.\n"
        "• Strict Evidence Contract: Every synthesized finding must cite a traceable EvidenceItem with file paths, excerpts, and UTC timestamps.",
        font_size=10.5,
    )

    # Comparison Table
    table_shape = slide.shapes.add_table(6, 5, Inches(0.7), Inches(3.68), Inches(11.9), Inches(2.82))
    table = table_shape.table

    table.columns[0].width = Inches(2.3)
    table.columns[1].width = Inches(2.3)
    table.columns[2].width = Inches(2.3)
    table.columns[3].width = Inches(2.3)
    table.columns[4].width = Inches(2.7)

    headers = ["Capability / Dimension", "Traditional RPA (UiPath/Selenium)", "Conversational AI (Perplexity/GPT)", "Autonomous Agents (Browser-Use)", "UNBOUND AI (Our Architecture)"]
    for col_idx, h in enumerate(headers):
        cell = table.cell(0, col_idx)
        cell.text = h
        cell.fill.solid()
        cell.fill.fore_color.rgb = ACCENT_PURPLE if col_idx == 4 else PRIMARY_NAVY
        p = cell.text_frame.paragraphs[0]
        p.font.name = "Calibri"
        p.font.size = Pt(10)
        p.font.bold = True
        p.font.color.rgb = WHITE
        p.alignment = PP_ALIGN.CENTER

    rows_data = [
        ("Workflow Creation", "Rigid manual scripting", "None (text search only)", "Dynamic prompt guessing", "Demonstration Learning + Parameter Replay"),
        ("Locator Resilience", "Brittle / breaks on class change", "N/A (no browser DOM action)", "High prompt/DOM token drift", "Self-healing multi-locator fallback ensemble"),
        ("Action Safety & HITL", "Rule-based script checks", "Read-only / no execution", "Unconstrained autonomous loop", "Cryptographic Human-in-the-Loop approval gate"),
        ("Doc & Context Fusion", "Separate third-party connectors", "Web text only", "Web DOM only", "Native Model Context Protocol (MCP) server"),
        ("Evidence Provenance", "System execution logs", "Web hyperlinks only", "Raw terminal / console dump", "Formal Pydantic EvidenceItem graph"),
    ]

    for row_idx, row in enumerate(rows_data, 1):
        for col_idx, val in enumerate(row):
            cell = table.cell(row_idx, col_idx)
            cell.text = val
            cell.fill.solid()
            if col_idx == 4:
                cell.fill.fore_color.rgb = RGBColor(243, 232, 255)
            else:
                cell.fill.fore_color.rgb = BG_CARD if row_idx % 2 == 0 else WHITE
            
            p = cell.text_frame.paragraphs[0]
            p.font.name = "Calibri"
            p.font.size = Pt(9.5)
            p.font.bold = (col_idx == 4 or col_idx == 0)
            p.font.color.rgb = ACCENT_PURPLE if col_idx == 4 else DARK_GRAY
            p.alignment = PP_ALIGN.CENTER if col_idx > 0 else PP_ALIGN.LEFT

    add_slide_footer(slide)


def setup_slide_7(slide):
    """Slide 7: Implementation Status, Feasibility & Risk Mitigation."""
    create_header_card(slide, "Implementation Status & Risk Mitigation", "Grounded Engineering Rigor: Implemented Modules, Verifications, and Active Work")
    clear_placeholder_shapes(slide)

    col_w = Inches(3.8)
    col_h = Inches(4.3)
    cgap = Inches(0.25)
    sx = Inches(0.7)
    sy = Inches(2.35)

    sections = [
        ("Implementation Status", ACCENT_PURPLE, [
            ("IMPLEMENTED & VERIFIED", "FastAPI gateway, Pydantic 2 contracts, TaskManager FSM, PyMuPDF/docx extractors, SQLite WAL store, TF-IDF ranking, MCP stdio server, Approval Gate logic (116 passing tests)."),
            ("IMPLEMENTED (UNVERIFIED)", "Playwright live browser replay across highly complex production SPAs; full live SSE stream binding to UI; Ollama 4B end-to-end latency benchmarks under concurrent load."),
            ("PLANNED / IN PROGRESS", "Automated background sync between local SQLite and AWS S3 bucket ('unbound-ai-documents-2026-karthik'); SQLite-vec local embeddings; self-healing visual locator fallbacks."),
        ]),
        ("Technical Feasibility", PRIMARY_NAVY, [
            ("Zero Cloud DB Lock-In", "SQLite WAL mode with sub-50ms TF-IDF chunk retrieval runs fully local without external vector database dependencies."),
            ("Local LLM Viability", "Optimized prompt templates tailored for local 4B/8B parameter models (Qwen 3.5 4B) minimize inference latency and compute overhead."),
            ("Deterministic Task Control", "TaskExecutor atomic lock prevents race conditions and ensures clean recovery and resumption upon failure."),
            ("Rigorous Test Suite", "116 automated unit and integration tests executing in ~1.85 seconds validate contracts and edge-case error handling."),
        ]),
        ("Risks & Engineering Mitigation", AMBER_TEXT, [
            ("Bot Detection & CAPTCHAs", "Mitigation: Gracefully detects anti-bot challenges, suspends automated loop, prompts user for manual completion, and resumes."),
            ("DOM Drift & Restructuring", "Mitigation: Multi-locator cascade (ARIA semantic roles > text > DOM tree hierarchy > coordinates) self-heals."),
            ("Unintended Destructive Action", "Mitigation: Approval engine halts execution on financial, messaging, or delete actions pending explicit user token sign-off."),
            ("Model Hallucinations", "Mitigation: Zero ungrounded synthesis; every claim strictly requires an EvidenceItem citation with source excerpts."),
        ]),
    ]

    for i, (title, color, items) in enumerate(sections):
        x = sx + i * (col_w + cgap)
        box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, sy, col_w, col_h)
        box.fill.solid()
        box.fill.fore_color.rgb = BG_CARD
        box.line.color.rgb = color
        box.line.width = Pt(1.5)

        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_top = Inches(0.14)
        tf.margin_left = Inches(0.16)
        tf.margin_right = Inches(0.16)

        p = tf.paragraphs[0]
        p.text = title
        p.font.name = "Calibri"
        p.font.size = Pt(13)
        p.font.bold = True
        p.font.color.rgb = color
        p.space_after = Pt(6)

        for subheader, desc in items:
            psh = tf.add_paragraph()
            psh.text = "▪ " + subheader
            psh.font.name = "Calibri"
            psh.font.size = Pt(10)
            psh.font.bold = True
            psh.font.color.rgb = color
            psh.space_before = Pt(3)

            pd = tf.add_paragraph()
            pd.text = desc
            pd.font.name = "Calibri"
            pd.font.size = Pt(9.2)
            pd.font.color.rgb = DARK_GRAY
            pd.space_after = Pt(3)

    add_slide_footer(slide)


def setup_slide_8(slide):
    """Slide 8: Practical Applicability & Business Potential."""
    create_header_card(slide, "Real-World Applicability & Business Potential", "Domain-Neutral Enterprise Utility, Token Economics, and Future Roadmap")
    clear_placeholder_shapes(slide)

    tb = slide.shapes.add_textbox(Inches(0.7), Inches(2.35), Inches(11.9), Inches(4.35))
    tf = tb.text_frame
    tf.word_wrap = True

    add_bullet_point(
        tf,
        "High-Impact Practical Use Cases",
        "• Enterprise Procurement & RFP Auditing: Automatically navigates vendor portals, extracts dynamic pricing tiers, cross-references internal SLA documents via MCP, and highlights discrepancy flags.\n"
        "• Regulatory Compliance & Filing Tracking: Monitors government and financial regulatory portals, extracts compliance changes, and compiles verifiable evidence reports.\n"
        "• Competitive Market & Academic Research: Gathers structured intelligence across paywalled or login-protected web sources without brittle scraper maintenance.\n"
        "• Digital Accessibility for Motor Impairments: Allows users with limited physical mobility to execute complex web transactions through plain conversational instructions.",
        is_first=True,
        font_size=11,
    )

    add_bullet_point(
        tf,
        "Sustainable Token Economics & Cost Advantage",
        "Unlike raw LLM browser agents that spend thousands of vision and reasoning tokens on every repeated web action, UNBOUND AI parameterizes demonstrated workflows. Once learned, recurring routines replay via deterministic Playwright execution, cutting token consumption by an estimated 80–90% on routine runs.",
        font_size=11,
    )

    add_bullet_point(
        tf,
        "Commercialization & Tiered Monetization Strategy",
        "• Open-Core Community Tier: Local MCP document server, local workflow recorder, local Ollama execution (free, self-hosted).\n"
        "• Professional Tier: Managed cloud execution runners, headless scheduling, automated cross-device workflow synchronization, and multi-browser support.\n"
        "• Enterprise Compliance Tier: On-premise deployment, team workflow library, cryptographic audit logs, SSO/RBAC, and custom enterprise MCP connectors.",
        font_size=11,
    )

    add_bullet_point(
        tf,
        "Development Roadmap",
        "Phase 1 (Completed): Core MCP server, document intelligence, SQLite WAL store, TaskManager state machine, 116 passing tests, and S3 connectivity.\n"
        "Phase 2 (Current): Seamless live UI integration, parameterized Playwright recording refinement, and background SQLite-to-S3 automatic backup.\n"
        "Phase 3 (Next): SQLite-vec dense semantic search, multi-tab coordination, and community-shared workflow repositories.",
        font_size=11,
    )

    add_slide_footer(slide)


def main():
    print(f"Loading template from: {TEMPLATE_PATH}")
    if not TEMPLATE_PATH.exists():
        print(f"ERROR: Template file not found at {TEMPLATE_PATH}")
        sys.exit(1)

    prs = pptx.Presentation(TEMPLATE_PATH)
    print(f"Template loaded successfully. Total slides: {len(prs.slides)}")

    # Update template layout footer shape to remove <Team Name> and <Team ID>
    layout = prs.slide_layouts[1]
    for s in layout.shapes:
        if s.has_text_frame and "Team Name" in s.text_frame.text:
            s.text_frame.clear()  # clear default layout footer to avoid any double text

    # Populate each slide
    print("Formatting Slide 1 (Title & Team)...")
    setup_slide_1(prs.slides[0])

    print("Formatting Slide 2 (Problem Statement)...")
    setup_slide_2(prs.slides[1])

    print("Formatting Slide 3 (Proposed Solution)...")
    setup_slide_3(prs.slides[2])

    print("Formatting Slide 4 (Tech Stack)...")
    setup_slide_4(prs.slides[3])

    print("Formatting Slide 5 (Process Flow / Architecture)...")
    setup_slide_5(prs.slides[4])

    print("Formatting Slide 6 (Novelty / Innovation)...")
    setup_slide_6(prs.slides[5])

    print("Formatting Slide 7 (Status & Feasibility)...")
    setup_slide_7(prs.slides[6])

    print("Formatting Slide 8 (Applicability & Business Model)...")
    setup_slide_8(prs.slides[7])

    # Save outputs
    print(f"Saving presentation to: {OUTPUT_PATH_1}")
    prs.save(str(OUTPUT_PATH_1))

    print(f"Saving copy to: {OUTPUT_PATH_2}")
    prs.save(str(OUTPUT_PATH_2))

    print("SUCCESS! Updated 8-slide presentation generated cleanly.")


if __name__ == "__main__":
    main()
