# Stack change SOP

Every tweak to the appliance is committed before the session that made it ends. The public repo is [hailports/StackMini](https://github.com/hailports/StackMini). Other checkouts get the same commit when they are a real project repo. A git root that is the home directory is never committed.

## When

- A server flag, prompt, grammar, sampler default, index hook, or overnight note shape changed.
- A doc in this repo is now wrong because of that change.
- Do this even when the change is one line.

## How

From a checkout of this repo:

```sh
scripts/stack-change.sh -m "Say what changed and why the output is better."
```

The script:

1. Refuses to run if the git root is the home directory.
2. Refuses a diff that contains a credential or a private host.
3. Commits only the paths you name. With no paths, it commits the whole dirty tree except `.env`.
4. Pushes when `origin` has a real push URL. A remote marked disabled is left as a local commit and the script says so.

Pass paths after `--` when the tree has unrelated edits:

```sh
scripts/stack-change.sh -m "Shorten overnight note lines." -- docs/MODEL.md
```

## What stays out

- Customer names, ticket ids, mail, and anything from a work index.
- Weights, logs, and `.env`.
- A second decode slot turned on while memory is tight. One slot is the default. Two slots are allowed only when the memory level is 40 or higher at server start, and only because output quality or an urgent stability problem required it. Speed stays the default.

## Other machines

The same script lives in the appliance bin on each seat. Run it in the checkout you edited. If that checkout cannot push, copy the public-safe summary into this repo and run the script here. Do not paste private notes into the summary.
