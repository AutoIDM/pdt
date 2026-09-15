Classify merge request !{iid} "{title}", branch `{source_branch}` targeting `{target_branch}`.

The branch is already fetched. Read the change with `git diff origin/{target_branch}...origin/{source_branch}` and open any file you need for context.

Deterministic rules set the floor tier **{floor}** because: {floor_reasons}.

Files changed:
{files}

End your reply with a fenced json block holding exactly `{{"tier": "simple" | "review" | "architectural", "reasons": ["..."]}}`.
