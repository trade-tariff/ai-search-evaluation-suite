import TraderJourneyApp from "../trader-journey/TraderJourneyApp";

// Native render of the end-to-end trader journey (previously a separate embedded app).
// TraderJourneyApp calls /api/* (proxied to the single :8000 backend) - no separate
// frontend/backend processes needed.
export default function TraderJourneyTab() {
  return <TraderJourneyApp />;
}
