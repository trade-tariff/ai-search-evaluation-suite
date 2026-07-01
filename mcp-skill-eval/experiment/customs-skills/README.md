# Customs Skills (Trade Tariff)

A small team of Agent Skills that turn Claude/ChatGPT into a virtual trade broker for an
infrequent ("nervous trader") importer/exporter. Each skill maps to one thing a trader
actually does, and translates broker/forwarder jargon into plain English.

## How these fit with the MCP

These skills are the **expertise layer**. They sit on top of the **GOV.UK Trade Tariff MCP
connector** (`mcp.trade-tariff.service.gov.uk`), which is the **data layer**.

- **Connector (MCP) = access.** Standardised tools over live UK tariff data.
- **Skills (these) = repeatable know-how.** They teach the agent *how* to use those tools the
  same careful way every time - which tool to call, in what order, how to read the result, and
  what to tell the trader next.

**These skills are MCP-connector-only.** They deliberately do not embed their own API calls or
guess tariff data from memory - commodity codes, duty rates, quotas and measures change
constantly and a wrong code or duty has real cost and legal consequences. If the connector is
not attached, each skill stops and tells the user how to add it.

## The skills here

| Skill | What it does | Key MCP tools |
|---|---|---|
| `guide-me` | Front door. Asks the trader's goal and routes to the right skill. | (orchestration) |
| `uk-commodity-code-classifier` | Finds the correct 10-digit commodity code using the GIRs. | `classification_search`, `note_mentions`, `navigate_hierarchy`, `lookup_commodity` |
| `duty-and-tax-estimator` | Estimates import duty + VAT / landed cost for a code + lane. | `lookup_commodity`, `list_geographical_areas`, `rules_of_origin`, `search_quotas`, `list_exchange_rates` |

See `CATALOGUE.md` for the full proposed skill set and which ones the tariff data actually backs.

## Adding the connector

- **Claude (desktop/web):** Settings -> Connectors -> add a custom connector with URL
  `https://mcp.trade-tariff.service.gov.uk/`. On first use it asks for a Hub `client_id` /
  `client_secret` (register free at `https://hub.trade-tariff.service.gov.uk/`).
- **Claude Desktop config (alternative):** add to `claude_desktop_config.json`:
  `{"mcpServers":{"trade-tariff":{"url":"https://mcp.trade-tariff.service.gov.uk/"}}}`

## Status

Proof-of-concept, built from the proven trader-journey classification prompts (GIRs, the
material/form/function/essential-character decomposition) and duty/landed-cost logic. Not yet
eval-gated end-to-end as skills - see the plan's verification section.
