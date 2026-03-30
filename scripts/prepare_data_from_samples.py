"""Helper script: copies the sample files (if available locally) into ./data.

Usage:
python scripts/prepare_data_from_samples.py --rulebook "/path/JSSD 2025.pdf" --templates "/path/Leave Certificate.docx" "/path/Movement  Order.docx"
"""
import argparse, os, shutil

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rulebook", required=True)
    ap.add_argument("--templates", nargs="+", required=True)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.join(args.out, "templates"), exist_ok=True)

    shutil.copyfile(args.rulebook, os.path.join(args.out, "rulebook.pdf"))
    for t in args.templates:
        shutil.copyfile(t, os.path.join(args.out, "templates", os.path.basename(t)))
    print("Prepared data in", args.out)

if __name__ == "__main__":
    main()
