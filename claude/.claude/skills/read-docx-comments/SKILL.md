---
name: read-docx-comments
description: Extract comments from a .docx file and show them with their anchored text context
argument-hint: "<path/to/file.docx>"
---

Extract all comments from the provided .docx file. The user uses this to give feedback on plans and documents by adding comments in Google Docs / Word.

## Steps

1. The user will provide a path to a .docx file. If not provided as an argument, ask for it.

2. Unzip the docx and extract both `word/comments.xml` and `word/document.xml` to /tmp.

3. Run this Python script to extract comments with their anchored text:

```python
import re, sys, xml.etree.ElementTree as ET

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

def get_text(elem):
    """Recursively extract all w:t text from an element."""
    parts = []
    for t in elem.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
        if t.text:
            parts.append(t.text)
    return ''.join(parts)

# Parse comments
comments = {}
tree = ET.parse('/tmp/word/comments.xml')
for c in tree.findall('.//w:comment', NS):
    cid = c.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id')
    author = c.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}author')
    date = c.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}date', '')
    text = get_text(c)
    comments[cid] = {'author': author, 'date': date, 'text': text, 'anchor': ''}

# Parse document to find anchored text for each comment
doc = ET.parse('/tmp/word/document.xml')
body = doc.getroot()

# Serialize back to string for regex-based range matching
# (ElementTree loses namespace prefixes, so re-read as string)
with open('/tmp/word/document.xml', 'r') as f:
    doc_str = f.read()

# For each comment, find text between commentRangeStart and commentRangeEnd
for cid in comments:
    start_pattern = f'commentRangeStart[^/]*w:id="{cid}"'
    end_pattern = f'commentRangeEnd[^/]*w:id="{cid}"'
    start_match = re.search(start_pattern, doc_str)
    end_match = re.search(end_pattern, doc_str)
    if start_match and end_match:
        between = doc_str[start_match.end():end_match.start()]
        texts = re.findall(r'<w:t[^>]*>(.*?)</w:t>', between)
        comments[cid]['anchor'] = ''.join(texts).strip()

# Output
for cid in sorted(comments.keys(), key=int):
    c = comments[cid]
    anchor = c['anchor']
    if anchor and len(anchor) > 120:
        anchor = anchor[:120] + '...'
    print(f"--- Comment {cid} ({c['author']}, {c['date'][:10]}) ---")
    if anchor:
        print(f"  On: \"{anchor}\"")
    print(f"  Comment: {c['text']}")
    print()
```

4. Present the comments in a clear format, grouped by their location in the document. For each comment, show:
   - The text the comment is anchored to (truncated if long)
   - The comment text
   - The author

5. After presenting, ask the user if they want you to act on the feedback.
