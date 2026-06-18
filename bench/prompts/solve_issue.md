You are solving a SWE-bench Lite task in a clean repository checkout.

Repository: {repo}
Base commit: {base_commit}
Instance: {instance_id}

Problem statement:

{problem_statement}

Target tests expected to fail before the fix and pass after the fix:

{fail_to_pass}

Regression tests expected to keep passing:

{pass_to_pass}

Instructions:

- Modify the repository to fix the issue described above.
- Keep the change focused on the issue.
- Use the target tests above to guide validation when practical.
- Do not commit changes.
- Do not read, print, or persist API keys or other environment secrets.
- Run relevant tests if practical, but prioritize producing a correct patch.
- When finished, stop after a brief summary.
