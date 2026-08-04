package server

import "testing"

func TestDisclosureThemeLabel(t *testing.T) {
	cases := map[string]string{
		"":                                     "Other",
		"ai_regulatory_compliance":             "AI regulatory compliance",
		"ai_cybersecurity_escalation":          "AI cybersecurity escalation",
		"supply_chain_operational_constraints": "Supply chain operational constraints",
		"tariffs_trade_policy":                 "Tariffs trade policy",
		"esg_reporting_pressure":               "ESG reporting pressure",
		"reit_distribution_risk":               "REIT distribution risk",
	}
	for in, want := range cases {
		if got := disclosureThemeLabel(in); got != want {
			t.Errorf("disclosureThemeLabel(%q) = %q, want %q", in, got, want)
		}
	}
}
