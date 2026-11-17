from devopsagents.domain.diff import parse_unified_diff

DIFF = """diff --git a/app/a.py b/app/a.py
index 111..222 100644
--- a/app/a.py
+++ b/app/a.py
@@ -1,3 +1,4 @@
 import os
-import sys
+import json
+import re
 x = 1
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
"""


def test_parses_files_and_flags():
    files = parse_unified_diff(DIFF)
    assert [f.path for f in files] == ["app/a.py", "img.png", "new.py"]
    assert files[1].is_binary
    assert files[2].is_new


def test_commentable_lines_use_new_side_numbers():
    f = parse_unified_diff(DIFF)[0]
    assert f.added_lines == {2: "import json", 3: "import re"}
    assert f.commentable_lines == {1, 2, 3, 4}  # context + added; removed line excluded
    assert f.changed_line_count == 3


def test_new_file_lines():
    f = parse_unified_diff(DIFF)[2]
    assert f.added_lines == {1: "a = 1", 2: "b = 2"}
