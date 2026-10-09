// duckdb-browser.mjs imports "apache-arrow" by bare name and the package
// ships no ES module that holds it, so the page loads Arrow's UMD script
// first and an import map sends the bare name here.
export const { Type, RecordBatchReader, Table, tableToIPC } = globalThis.Arrow;
