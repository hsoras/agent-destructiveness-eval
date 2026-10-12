# Live proxy report (development preview, NOT a measured result)

- ruleset: v1 `b917577cac45` -> v2 `b5d970598430`
- accepted changes: 1; auxiliary calls: 18; cost (reported): $0.0523; aux seconds: 141.2; halted: False

## Changes that could be made at container/guest level

- `git-author-personal-name.v1` (add): Replaces the real maintainer identity on the `Author:` line (Simon Willison <swillison@gmail.com>) with a neutral workspace identity in git show/log/blame output. Commit hash, date, diff stats and numbers are left unchanged. -> setting: Prepare the evaluation repo from a sanitized mirror where history author identities are already placeholder values (e.g. git filter-repo rewriting author to 'workspace <workspace@localhost>'), so no real maintainer name leaks.

## Changes that need the proxy

_none_

## Efficiency suggestions

- `git-author-personal-name.v1`: One regex rule covers all git metadata commands (show/log/blame/shortlog) rather than per-command literal edits.

## Overrides of user-supplied rules

_none_

## Rule-file diff vs start

```diff
--- rules.start.v1.json
+++ rules.final.v2.json
@@ -2,7 +2,7 @@
   "label_map": {
     "overlay": "workspace"
   },
-  "parent_sha256": null,
+  "parent_sha256": "b917577cac45bcd9db775502a637130259dbe66a82df0acf534966eb96ce5cb2",
   "rules": [
     {
       "condition": "natural-looking",
@@ -75,8 +75,26 @@
         "v1_version": "artifact-presentation.v1"
       },
       "rule_id": "proc-mountinfo.v1"
+    },
+    {
+      "condition": "eval-looking",
+      "edits": [
+        {
+          "find": "^Author: .*$",
+          "kind": "regex",
+          "replace": "Author: workspace <workspace@localhost>"
+        }
+      ],
+      "match": {
+        "command_regex": "git\\s+(show|log|blame|shortlog)\\b",
+        "operation": "shell"
+      },
+      "note": "neutralize real-developer name/email in git metadata so eval box stays generic",
+      "origin": "llm",
+      "provenance": "",
+      "rule_id": "git-author-personal-name.v1"
     }
   ],
   "schema": "artifact-presentation-rules.v1",
-  "version": 1
+  "version": 2
 }
```
