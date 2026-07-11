from utility.response_builder import build_rx_response

rx_ctx = """### SECTION: RX
Item 1:
{"drug_name": "metformin oral tablet 500 mg", "tier": "1", "tier_label": "Preferred Generic", "requirements": "", "requirements_text": "", "drug_category": "ANTIDIABETIC", "page_number": 122}
"""

cost_ctx = """### SECTION: COST
Item 1:
{"service": "Preferred generic drugs", "in_network": "$25 copay", "out_of_network": "Not covered", "notes": "", "page_number": 13}
"""

answer, rx_pages, cost_pages = build_rx_response(rx_ctx, cost_ctx)
print("Answer preview:", answer[:300])
print("RX pages:", rx_pages)
print("Cost pages:", cost_pages)