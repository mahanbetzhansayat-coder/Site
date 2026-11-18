"""PDF-сертификат о прохождении курса (A4, альбомная ориентация)."""
import os
from datetime import date
from io import BytesIO

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

SITE_NAME = "LearnHub"

FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(FONT_DIR, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")))

PRIMARY = HexColor("#5b5bd6")
VIOLET = HexColor("#8b5cf6")
SOFT = HexColor("#eef0ff")
TEXT = HexColor("#111827")
MUTED = HexColor("#6b7280")
LINE = HexColor("#d1d5db")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def _fit(c, text, font, size, max_width, min_size=14):
    """Уменьшает шрифт, пока текст не поместится по ширине."""
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def build_certificate(student_name, course_title, instructor=None, score=None,
                      number="", issued=None):
    issued = issued or date.today()
    buf = BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setTitle(f"Сертификат — {course_title}")
    c.setAuthor(SITE_NAME)

    # Фон, угловые акценты и рамки
    c.setFillColor(white)
    c.rect(0, 0, width, height, stroke=0, fill=1)
    c.setFillColor(SOFT)
    for x, y in [(34, height - 34), (width - 34, 34)]:
        c.circle(x, y, 90, stroke=0, fill=1)
    c.setStrokeColor(PRIMARY)
    c.setLineWidth(6)
    c.rect(22, 22, width - 44, height - 44, stroke=1, fill=0)
    c.setStrokeColor(VIOLET)
    c.setLineWidth(1)
    c.rect(34, 34, width - 68, height - 68, stroke=1, fill=0)

    cx = width / 2

    # Логотип
    c.setFillColor(PRIMARY)
    c.roundRect(cx - 22, height - 112, 44, 44, 10, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("DejaVu-Bold", 24)
    c.drawCentredString(cx, height - 99, "✦")
    c.setFillColor(TEXT)
    c.setFont("DejaVu-Bold", 14)
    c.drawCentredString(cx, height - 132, SITE_NAME)

    # Заголовок
    c.setFillColor(PRIMARY)
    c.setFont("DejaVu-Bold", 44)
    c.drawCentredString(cx, height - 190, "СЕРТИФИКАТ")
    c.setFillColor(MUTED)
    c.setFont("DejaVu", 14)
    c.drawCentredString(cx, height - 214, "о прохождении онлайн-курса")

    # Имя
    c.setFont("DejaVu", 13)
    c.drawCentredString(cx, height - 262, "Настоящим подтверждается, что")
    size = _fit(c, student_name, "DejaVu-Bold", 34, width - 220)
    c.setFillColor(TEXT)
    c.setFont("DejaVu-Bold", size)
    c.drawCentredString(cx, height - 306, student_name)
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.line(cx - 220, height - 320, cx + 220, height - 320)

    # Курс
    c.setFillColor(MUTED)
    c.setFont("DejaVu", 13)
    c.drawCentredString(cx, height - 348, "успешно завершил(а) курс")
    size = _fit(c, f"«{course_title}»", "DejaVu-Bold", 24, width - 220)
    c.setFillColor(PRIMARY)
    c.setFont("DejaVu-Bold", size)
    c.drawCentredString(cx, height - 382, f"«{course_title}»")

    # Нижний блок: дата, балл, номер
    issued_text = f"{issued.day} {MONTHS[issued.month - 1]} {issued.year} г."
    columns = [
        ("Дата выдачи", issued_text),
        ("Средний балл по тестам", f"{score}%" if score is not None else "—"),
        ("Номер сертификата", number or "—"),
    ]
    col_w = (width - 200) / len(columns)
    for i, (label, value) in enumerate(columns):
        x = 100 + col_w * i + col_w / 2
        c.setFillColor(MUTED)
        c.setFont("DejaVu", 10)
        c.drawCentredString(x, 118, label)
        c.setFillColor(TEXT)
        c.setFont("DejaVu-Bold", 14)
        c.drawCentredString(x, 98, value)

    if instructor:
        c.setFillColor(MUTED)
        c.setFont("DejaVu", 10)
        c.drawCentredString(cx, 62, f"Преподаватель курса: {instructor}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
