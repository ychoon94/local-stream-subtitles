# Test quality

- Before adding a test, identify the specific observable behavior, invariant, or
  contract, a credible mistake it should catch, and an independently justified
  expected result.
- Prefer a small suite of fast, reliable tests whose failures identify the broken
  behavior. Use integration/E2E tests for interactions that isolated tests cannot
  establish. Coverage numbers alone do not justify a test.
- Do not reproduce implementation logic in assertions or mocks, compute circular
  expectations, or use source/import/string greps as proxies for behavior unless
  they enforce an explicit contract.
- Protect contracts, not incidental internal structure. Assertions should survive
  behavior-preserving refactors beneath the protected contract; routine import or
  setup updates are acceptable.
- Retain tests only when they add a distinct failure mode, important edge case, or
  substantially faster, more reliable, or more precise feedback than retained
  alternatives. Read candidates, production owners, and claimed replacements
  before pruning. Resolve consequential redundancy uncertainty with a
  representative fault or retain the test and report the uncertainty.
- Distinguish temporary development scaffolding from checks worth maintaining.
  Remove obsolete probes and newly orphaned test-only helpers or seams after
  checking consumers; preserve runtime behavior and public APIs.
- Never delete or weaken a failing test just to make the suite pass. Record
  pre-existing failures and investigate new retained-test failures as possible
  product bugs.
