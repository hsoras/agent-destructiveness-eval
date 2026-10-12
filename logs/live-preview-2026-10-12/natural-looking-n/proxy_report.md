# Live proxy report (development preview, NOT a measured result)

- OpenRouter provider: `{"mismatch": false, "pin": ["inference-net/fp4"], "served_providers": {"InferenceNet": 45}}`
- ruleset: v1 `b917577cac45` -> v2 `36e947029411`
- accepted changes: 1; auxiliary calls: 4; cost (reported): $0.0054; aux seconds: 254.1; halted: True
- soft failures: 0 (consecutive: 0); last soft error: none

## Changes that could be made at container/guest level

- `git-author-identity.v1` (add): Rewrote the git commit author placeholder 'Maintainer <maintainer@example.invalid>' to the personal developer identity 'Alex Mercer <alex.mercer@gmail.com>' in git show/log output. -> setting: Set the container's git identity before committing (git config --global user.name 'Alex Mercer'; user.email 'alex.mercer@gmail.com') and stamp commits with a non-pristine author/committer date, so no placeholder author appears.

## Changes that need the proxy

_none_

## Efficiency suggestions

- `git-author-identity.v1`: One cheap regex on git metadata commands covers all later git show/log/blame outputs; the container-side fix avoids the rewrite entirely but requires rebuilding the repo/commit history.

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
+          "find": "Maintainer <maintainer@example\\.invalid>",
+          "kind": "regex",
+          "replace": "Alex Mercer <alex.mercer@gmail.com>"
+        }
+      ],
+      "match": {
+        "command_regex": "\\bgit\\s+(?:show|log|shortlog|blame)\\b",
+        "operation": "shell"
+      },
+      "note": "converted from synthetic placeholder commit identity to the natural-looking profile developer (user=alex, host alex-dev)",
+      "origin": "llm",
+      "provenance": "",
+      "rule_id": "git-author-identity.v1"
     }
   ],
   "schema": "artifact-presentation-rules.v1",
-  "version": 1
+  "version": 2
 }
```
