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

# --- Generate 2024 Data (PPO Silo) ---
# We add "Data Not Found" to force the LLM to stay in this year's box.
create_pdf(
    "./docs/2024",
    "Medical_Gold.pdf",
    [
        "Plan Year: 2024",
        "Plan Name: 2024 Premera Gold Medical (PPO)",
        "Tier: Gold",
        "Benefit | In-Network | Out-of-Network",
        "Annual Deductible | $500 Individual / $1,000 Family | Data Not Found",
        "Primary Care Physician (PCP) | $25 copay | Data Not Found",
        "Specialist Physician | $50 copay | Data Not Found",
        "---",
        "The individual deductible for the 2024 Gold Medical Plan is $500.",
        "The family deductible for the 2024 Gold Medical Plan is $1,000.",
    ],
)

# --- Generate 2025 Data (PPO Silo) ---
create_pdf(
    "./docs/2025",
    "Medical_Gold.pdf",
    [
        "Plan Year: 2025",
        "Plan Name: 2025 Premera Gold Medical (PPO)",
        "Tier: Gold",
        "Benefit | In-Network | Out-of-Network",
        "Annual Deductible | $750 Individual / $1,500 Family | Data Not Found",
        "Primary Care Physician (PCP) | $35 copay | Data Not Found",
        "Specialist Physician | $60 copay | Data Not Found",
        "---",
        "The individual deductible for the 2025 Gold Medical Plan is $750.",
        "The family deductible for the 2025 Gold Medical Plan is $1,500.",
    ],
)

# --- Generate 2025 Dental Data (Topic Isolation Test) ---
# This ensures "Dental Deductibles" don't leak into "Medical" queries.
create_pdf(
    "./docs/2025",
    "Dental_Silver.pdf",
    [
        "Plan Year: 2025",
        "Plan Name: 2025 Premera Silver Dental (PPO)",
        "Tier: Silver",
        "Topic: Orthodontia Benefits",
        "Individual Deductible | $50 | Data Not Found",
        "This Silver Dental plan covers braces at 50% up to a $1500 lifetime max.",
    ],
)

if __name__ == "__main__":
    print("Surgical PDF Test Suite generated with explicit Out-of-Network silos.")
