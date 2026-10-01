Classify pull request #{number} "{title}", branch `{head_ref}` targeting `{base_ref}`.

The branch is already fetched. Read the change with `git diff origin/{base_ref}...origin/{head_ref}` and open any file you need for context.

Deterministic rules set the floor tier **{floor}** because: {floor_reasons}.

Files changed:
{files}

End your reply with a fenced json block holding exactly `{{"tier": "simple" | "review" | "architectural", "reasons": ["..."]}}`.
