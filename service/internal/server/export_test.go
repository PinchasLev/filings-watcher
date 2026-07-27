package server

// Test hooks exporting unexported helpers to the external server_test package.

// FirstSentenceForTest exposes firstSentence for edge-case unit testing.
var FirstSentenceForTest = firstSentence
