# Bug: MCP `note_mentions` returns 422 for every commodity (prod)

**Component:** `trade-tariff/mcp` `note_mentions` tool -> Trade Tariff backend
`/uk/api/v2/knowledge_graph/queries`
**Env:** prod MCP `https://mcp.trade-tariff.service.gov.uk/`; backend = whatever `TARIFF_API_URL`
points at in prod. **Date:** 2026-06-24.

## Summary

`note_mentions` fails for all inputs with a backend **422**. The MCP request and the backend code
are both correct - the root cause is a **backend KG data/state** problem: no subject resolves to a
graph node on the production backend the MCP calls. The classification workflow's "notes" step is
therefore dead on prod (agents silently fall back to the hierarchy).

(RCA independently reviewed by codex on 2026-06-24: "partially correct, correct in spirit" -
confirmed it is a backend KG data/state issue, **not** an MCP request/identifier/routing bug;
flagged that "tables are empty" is an overclaim until prod node counts are checked. Refinements
incorporated below.)

## Reproduce

**A. Straight at the backend (no MCP, no auth - open `www` host) with the exact body the MCP sends:**

```bash
curl -s -X POST "https://www.trade-tariff.service.gov.uk/uk/api/v2/knowledge_graph/queries" \
  -H "Accept: application/vnd.hmrc.2.0+json" -H "Content-Type: application/json" \
  -d '{"data":{"type":"knowledge_graph_query","attributes":{"preset":"note_mentions",
       "subjects":[{"type":"goods_nomenclature","identifiers":{"goods_nomenclature_item_id":"8518300010"}}],
       "include":["nodes","edges","content"]}}}'
```

Observed (HTTP 422):

```json
{"errors":[{"status":"422","title":"Invalid knowledge graph query",
  "detail":"subjects must resolve at least one graph node",
  "source":{"pointer":"/data/attributes/subjects"}}]}
```

Tried multiple valid declarable codes + the `goods_nomenclature_sids` form - same 422.

**B. Via the MCP** (Hub `client_credentials` token -> Bearer):

```bash
curl -s -X POST https://mcp.trade-tariff.service.gov.uk/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"note_mentions",
       "arguments":{"goods_nomenclature_item_ids":["8518300010"],"service":"uk"}}}'
```

Returns: `Internal error calling tool note_mentions: Backend API error: API error 422:
/uk/api/v2/knowledge_graph/queries` (the MCP collapses the backend's detail - see fix 2).

## Root cause analysis (from the public repos)

1. **MCP request is well-formed.** `app/tools/note_mentions_tool.rb` POSTs
   `{data:{type:"knowledge_graph_query",attributes:{preset:"note_mentions",subjects:[...],include:[nodes,edges,content]}}}`
   with subjects `{type:"goods_nomenclature",identifiers:{goods_nomenclature_item_id: <id>}}`. This
   matches the endpoint contract.

2. **Backend supports `goods_nomenclature_item_id`.** In
   `trade-tariff-backend app/services/tariff_knowledge/graph_query.rb#resolve_goods_nomenclature`:
   ```ruby
   clauses << { goods_nomenclature_sid: identifiers['goods_nomenclature_sid'].to_i } if ...present?
   clauses << { goods_nomenclature_item_id: identifiers['goods_nomenclature_item_id'].to_s } if ...present?
   ...map { |clause| dataset.where(clause) }   # dataset = Node.goods_nomenclatures
   ```
   So item_id is a valid identifier; the request is not the problem.

3. **The 422 is a no-match, not a validation error.** Same file:
   ```ruby
   errors << error('/data/attributes/subjects', 'subjects must resolve at least one graph node') if subject_nodes.empty?
   ```
   `subject_nodes` comes from `Node.goods_nomenclatures.where(goods_nomenclature_item_id: ...)`. It is
   empty -> **there is no `tariff_knowledge_node` row for that commodity on the prod backend.**

4. **Conclusion:** no `tariff_knowledge_node` resolves for these commodities on the production
   backend - a backend **data/state** issue, not an MCP or backend *code* bug. (codex reproduced the
   same 422 across the `goods_nomenclature_item_id`, `goods_nomenclature_sid`, AND `node_key` request
   forms, which rules out an identifier/request-shape cause.) Leading hypotheses - a prod DB count
   disambiguates them, so don't assert "empty" until checked:
   - the KG loader (`app/services/tariff_knowledge/declarable_node_loader.rb` + the source-graph
     loaders) hasn't run or failed on prod;
   - declarable goods nodes specifically are missing (source graph loaded but not declarables);
   - the MCP's `TARIFF_API_URL` points at a backend/env/DB without the KG loaded;
   - UK/XI data mismatch, or prod != public `main`.
   The passing backend spec (`spec/requests/api/v2/knowledge_graph/queries_controller_spec.rb`) only
   works because it `create`s the nodes/edges in the test DB.

## Suggested fixes (ranked)

1. **(Backend/infra - the actual fix)** First **verify prod KG state** on the backend the MCP's
   `TARIFF_API_URL` resolves to:
   `SELECT count(*) FROM tariff_knowledge_nodes;` and
   `SELECT count(*) FROM tariff_knowledge_nodes WHERE goods_nomenclature_item_id = '8518300010';`
   If empty/missing, **run the KG loader** (`declarable_node_loader` + the source-graph loaders) on
   prod, **or** point `TARIFF_API_URL` at the environment where the KG is already loaded.
2. **(MCP DX)** Surface the backend's error detail instead of collapsing it. In
   `app/tools/application_tool.rb#with_error_handling`, the `ApiError` branch raises a generic
   "Backend API error: ...". Passing through the backend's `errors[].detail`
   ("subjects must resolve at least one graph node") would make this self-diagnosing.
3. **(Optional robustness)** `classification_search` already returns `goods_nomenclature_sid`;
   `note_mentions` could pass `goods_nomenclature_sids` (the canonical identifier in the backend
   spec) alongside item_ids. Not the cause here, but a more robust subject key.

## Net
MCP code: correct. Backend code: correct. Missing piece: the KG node/edge data on the prod backend.
Until it's seeded there, `note_mentions` returns 422 for everything.
