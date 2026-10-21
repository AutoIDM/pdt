// Sample Docker app in Node.js. Run it with `pdt run <name>`.
//
// pdt gives the program the app's config: section as JSON in PDT_CONFIG_JSON,
// and keeps every file it writes to PDT_OUTPUT_DIR in the app's data store.

const fs = require("fs");
const path = require("path");

const config = JSON.parse(process.env.PDT_CONFIG_JSON || "{}");
const greeting = config.greeting || "Hello, world!";
console.log(greeting);

if (process.env.PDT_OUTPUT_DIR) {
  fs.writeFileSync(path.join(process.env.PDT_OUTPUT_DIR, "greeting.txt"), greeting + "\n");
}
