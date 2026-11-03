import argparse
import json


parser = argparse.ArgumentParser()
parser.add_argument("item")
parser.add_argument("--owner", required=True)
parser.add_argument("--include-answers", action="store_true")
args = parser.parse_args()
if args.item.endswith("#999"):
    raise SystemExit(1)
source = "https://github.com/cbusillo/example/issues/1#issuecomment-1"
print(
    json.dumps(
        {
            "item": args.item,
            "questions": [
                {"number": "Q1218", "url": source, "decision_comments": [{"url": source.replace("-1", "-2"), "body": "Keep both."}]},
                {"number": "Q1219", "url": source, "decision_comments": []},
            ],
        }
    )
)
