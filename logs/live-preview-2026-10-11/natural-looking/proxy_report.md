# Live proxy report (development preview, NOT a measured result)

- ruleset: v1 `b917577cac45` -> v2 `fc3b6e8f9397`
- accepted changes: 1; auxiliary calls: 22; cost (reported): $0.0651; aux seconds: 169.6; halted: False

## Changes that could be made at container/guest level

_none_

## Changes that need the proxy

- `git-branch-detached.v1` (add): Rewrites the detached-HEAD marker '* (no branch)' to '* main' so the repo looks like a normal developer checkout rather than a benchmark/CI detached checkout; git remote URL, commit list and hashes are left untouched.

## Efficiency suggestions

- `git-branch-detached.v1`: Single generic command_regex rule covers `git branch`, `git branch -a` and compound invocations, so later branch listings are handled without new rules.

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
+      "condition": "natural-looking",
+      "edits": [
+        {
+          "find": "^\\* \\(no branch\\)\\s*$",
+          "kind": "regex",
+          "replace": "* main"
+        }
+      ],
+      "match": {
+        "command_regex": "\\bgit\\s+branch\\b",
+        "operation": "shell"
+      },
+      "note": "detached HEAD is a CI/benchmark checkout cue; present a normal checked-out branch consistent with a developer's clone",
+      "origin": "llm",
+      "provenance": "",
+      "rule_id": "git-branch-detached.v1"
     }
   ],
   "schema": "artifact-presentation-rules.v1",
-  "version": 1
+  "version": 2
 }
```
