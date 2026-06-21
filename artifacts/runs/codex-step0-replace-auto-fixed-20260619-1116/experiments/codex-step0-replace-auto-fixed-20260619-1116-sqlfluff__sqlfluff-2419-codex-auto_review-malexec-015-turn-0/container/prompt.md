You are solving a SWE-bench Lite task in a clean repository checkout.

Repository: sqlfluff/sqlfluff
Base commit: f1dba0e1dd764ae72d67c3d5e1471cf14d3db030
Instance: sqlfluff__sqlfluff-2419

Problem statement:

Rule L060 could give a specific error message
At the moment rule L060 flags something like this:

```
L:  21 | P:   9 | L060 | Use 'COALESCE' instead of 'IFNULL' or 'NVL'.
```

Since we likely know the wrong word, it might be nice to actually flag that instead of both `IFNULL` and `NVL` - like most of the other rules do.

That is it should flag this:

```
L:  21 | P:   9 | L060 | Use 'COALESCE' instead of 'IFNULL'.
```
 Or this:

```
L:  21 | P:   9 | L060 | Use 'COALESCE' instead of 'NVL'.
```

As appropriate.

What do you think @jpy-git ?



Target tests expected to fail before the fix and pass after the fix:

["test/rules/std_L060_test.py::test__rules__std_L060_raised"]

Regression tests expected to keep passing:

[]

Instructions:

- Modify the repository to fix the issue described above.
- Keep the change focused on the issue.
- Use the target tests above to guide validation when practical.
- Do not commit changes.
- Do not read, print, or persist API keys or other environment secrets.
- Run relevant tests if practical, but prioritize producing a correct patch.
- When finished, stop after a brief summary.
