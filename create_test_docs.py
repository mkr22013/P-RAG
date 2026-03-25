import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

def create_pdf(folder, filename, content):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    c = canvas.Canvas(path, pagesize=letter)
    
    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(100, 750, f"Official Document: {filename}")
    
    # Body Content
    c.setFont("Helvetica", 12)
    text_object = c.beginText(100, 720)
    for line in content:
        text_object.textLine(line)
    c.drawText(text_object)
    
    c.showPage()
    c.save()
    print(f"Created: {path}")

# --- Generate 2024 Data ---
create_pdf("./docs/2024", "Medical_Gold.pdf", [
    "Plan Year: 2024",
    "Plan Type: Medical",
    "Tier: Gold",
    "Topic: Annual Deductible",
    "The individual deductible for the 2024 Gold Medical Plan is $500.",
    "The family deductible for the 2024 Gold Medical Plan is $1000.",
    "Primary Care Physician (PCP) co-pay is $25."
])

# --- Generate 2025 Data (Updated prices for comparison testing) ---
create_pdf("./docs/2025", "Medical_Gold.pdf", [
    "Plan Year: 2025",
    "Plan Type: Medical",
    "Tier: Gold",
    "Topic: Annual Deductible",
    "The individual deductible for the 2025 Gold Medical Plan is $750.",
    "The family deductible for the 2025 Gold Medical Plan is $1500.",
    "Primary Care Physician (PCP) co-pay is $35."
])

create_pdf("./docs/2025", "Dental_Silver.pdf", [
    "Plan Year: 2025",
    "Plan Type: Dental",
    "Tier: Silver",
    "Topic: Orthodontia Benefits",
    "Keywords: braces, dental, orthodontics, silver",
    "This Silver Dental plan covers braces at 50% up to a $1500 lifetime max."
])