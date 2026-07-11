"""Quick script to check what SAB values exist in RXNCONSO.RRF and RXNREL.RRF"""

import sys
from collections import Counter

rrf_dir = sys.argv[1] if len(sys.argv) > 1 else "."

# Check RXNCONSO SAB values
print("=== RXNCONSO.RRF SAB values ===")
sab_counts = Counter()
with open(f"{rrf_dir}/RXNCONSO.RRF", encoding="utf-8") as f:
    for line in f:
        parts = line.split("|")
        if len(parts) > 11:
            sab_counts[parts[11].strip()] += 1

for sab, count in sorted(sab_counts.items(), key=lambda x: -x[1])[:20]:
    print(f"  {sab}: {count:,}")

# Check RXNREL SAB values
print("\n=== RXNREL.RRF SAB values ===")
sab_counts2 = Counter()
rela_counts = Counter()
with open(f"{rrf_dir}/RXNREL.RRF", encoding="utf-8") as f:
    for line in f:
        parts = line.split("|")
        if len(parts) > 10:
            sab_counts2[parts[10].strip()] += 1
            if parts[7].strip() in ("may_treat", "may_prevent", "has_ingredient"):
                rela_counts[f"{parts[10].strip()}.{parts[7].strip()}"] += 1

for sab, count in sorted(sab_counts2.items(), key=lambda x: -x[1])[:20]:
    print(f"  {sab}: {count:,}")

print("\n=== Key relations ===")
for rel, count in sorted(rela_counts.items(), key=lambda x: -x[1]):
    print(f"  {rel}: {count:,}")
