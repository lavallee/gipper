# Vast.ai sources

Pages captured on 2026-10-27 while building placement (each JSON holds the URL,
the time it was fetched, and the extracted text). They back the claims in
[docs/placement.md](../../placement.md) about Vast's units and billing:

- `billing.json`: rental billed by the second while running, storage for every
  second the instance exists, bandwidth by the byte
  (https://raw.githubusercontent.com/vast-ai/docs/main/guides/reference/billing.mdx)
- `search-offers.json`: the offer search schema, including the `inet_down_cost`
  and `inet_up_cost` fee fields (https://docs.vast.ai/api-reference/search/search-offers)
- `cli-source.json`: the official CLI, which reads `inet_down`/`inet_up` as Mb/s
  and fees as $/GB (https://github.com/vast-ai/vast-cli/blob/master/vast.py)
- `choosing.json`: choosing instances (https://docs.vast.ai/documentation/instances/choosing)

The metrics and performance pages couldn't be fetched then.
