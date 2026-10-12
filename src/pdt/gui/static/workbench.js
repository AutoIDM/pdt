// The SQL workbench of a run's CSV files. Every file is a DuckDB view in the
// browser; the page state below compiles to one query, and render() runs it.
const root = document.getElementById("workbench");
const files = JSON.parse(document.getElementById("files").textContent);
const byView = new Map(files.map((file) => [file.view, file]));
const PAGE_SIZE = 200;
const state = {mode: "simple", active: root.dataset.active, sql: "", sort: null, filter: "",
               page: 0, pageSize: PAGE_SIZE};

const el = (selector) => root.querySelector(selector);
const modeButtons = root.querySelectorAll("[data-mode]");
const fileSelect = el("[data-file]");
const filterInput = el("[data-filter]");
const statusText = el("[data-status]");
const sqlPanel = el(".wb-sql");
const sqlInput = el("[data-sql]");
const commandLine = el("[data-command-line]");
const errorBox = el("[data-error]");
const head = el("thead");
const body = el("tbody");
const rowsText = el("[data-rows]");
const prevButton = el("[data-prev]");
const nextButton = el("[data-next]");

const ident = (name) => `"${String(name).replace(/"/g, '""')}"`;
const literal = (text) => `'${String(text).replace(/'/g, "''")}'`;

const ARROW_TYPES = {Utf8: "VARCHAR", LargeUtf8: "VARCHAR", Bool: "BOOLEAN", Int8: "TINYINT",
                     Int16: "SMALLINT", Int32: "INTEGER", Int64: "BIGINT", Uint8: "UTINYINT",
                     Uint16: "USMALLINT", Uint32: "UINTEGER", Uint64: "UBIGINT",
                     Float32: "FLOAT", Float64: "DOUBLE", Binary: "BLOB"};

function typeName(type) {
    const name = String(type);
    if (name.startsWith("Date")) return "DATE";
    if (name.startsWith("Timestamp")) return "TIMESTAMP";
    if (name.startsWith("Decimal")) return name.replace(/^Decimal\[(\d+)e-(\d+)\]/, "DECIMAL($1,$2)");
    if (name.startsWith("Time")) return "TIME";
    return ARROW_TYPES[name] || name;
}

function formatter(type) {
    const name = String(type);
    const stamp = (value) => new Date(Number(value)).toISOString().replace("T", " ").replace(/\.?0*Z$/, "");
    if (name.startsWith("Date")) return (value) => stamp(value).slice(0, 10);
    if (name.startsWith("Timestamp")) return stamp;
    return (value) => {
        if (typeof value === "bigint") return value.toString();
        if (value instanceof Uint8Array) return `${value.length} bytes`;
        if (typeof value === "object") {
            return JSON.stringify(value, (_key, item) => typeof item === "bigint" ? item.toString() : item);
        }
        return String(value);
    };
}

let db = null;
let conn = null;
const columnsOf = new Map();
let result = {fields: [], rows: [], total: 0, ms: 0};
let generation = 0;

async function columns(view) {
    if (!columnsOf.has(view)) {
        const described = await conn.query(`DESCRIBE ${ident(view)}`);
        columnsOf.set(view, described.toArray().map((row) => row.toJSON().column_name));
    }
    return columnsOf.get(view);
}

async function compile(s) {
    const from = ident(s.active);
    const where = s.filter
        ? ` WHERE ${(await columns(s.active)).map((column) =>
            `CAST(${ident(column)} AS VARCHAR) ILIKE ${literal(`%${s.filter}%`)}`).join("\n   OR ")}`
        : "";
    const order = s.sort ? ` ORDER BY ${ident(s.sort.column)} ${s.sort.dir.toUpperCase()}` : "";
    const offset = s.page ? ` OFFSET ${s.page * s.pageSize}` : "";
    return {query: `SELECT * FROM ${from}${where}${order} LIMIT ${s.pageSize}${offset}`,
            count: `SELECT count(*) AS n FROM ${from}${where}`};
}

const viewPattern = new RegExp(files.map((file) => `"${file.view}"|\\b${file.view}\\b`).join("|"), "g");

function command(sql) {
    const text = sql.replace(viewPattern, (match) => literal(byView.get(match.replace(/"/g, "")).path));
    return `${root.dataset.command} "${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

async function render(s) {
    if (!conn) return;
    const mine = ++generation;
    const simple = s.mode === "simple";
    modeButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === s.mode)));
    sqlPanel.hidden = simple;
    filterInput.hidden = !simple;
    fileSelect.value = s.active;
    if (simple) {
        const compiled = await compile(s);
        s.sql = compiled.query;
        await run(mine, compiled.query, compiled.count);
    } else {
        sqlInput.value = s.sql;
        commandLine.textContent = command(s.sql);
        await run(mine, s.sql, null);
    }
}

async function run(mine, query, count) {
    if (!conn) return;
    errorBox.hidden = true;
    statusText.textContent = "running…";
    const started = performance.now();
    try {
        const table = await conn.query(query);
        const rows = table.toArray().map((row) => row.toJSON());
        const total = count ? Number((await conn.query(count)).toArray()[0].toJSON().n) : rows.length;
        if (mine !== generation) return;
        result = {fields: table.schema.fields, rows, total, ms: Math.round(performance.now() - started)};
    } catch (error) {
        if (mine !== generation) return;
        result = {fields: [], rows: [], total: 0, ms: Math.round(performance.now() - started)};
        errorBox.textContent = String(error.message || error);
        errorBox.hidden = false;
    }
    if (state.mode === "sql" && state.page * state.pageSize >= result.total) state.page = 0;
    draw();
}

function draw() {
    const simple = state.mode === "simple";
    const shown = simple ? result.rows : result.rows.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
    const first = result.total ? state.page * state.pageSize + 1 : 0;
    const last = state.page * state.pageSize + shown.length;
    head.replaceChildren();
    body.replaceChildren();
    const headRow = document.createElement("tr");
    const formats = result.fields.map((field) => formatter(field.type));
    for (const field of result.fields) {
        const th = document.createElement("th");
        const name = document.createElement("span");
        name.textContent = field.name;
        const type = document.createElement("span");
        type.className = "wb-type";
        type.textContent = typeName(field.type);
        th.append(name, " ", type);
        if (simple) {
            th.className = "wb-sortable";
            if (state.sort && state.sort.column === field.name) {
                th.append(state.sort.dir === "asc" ? " ▲" : " ▼");
                th.setAttribute("aria-sort", state.sort.dir === "asc" ? "ascending" : "descending");
            }
            th.addEventListener("click", () => sortBy(field.name));
        }
        headRow.append(th);
    }
    head.append(headRow);
    for (const row of shown) {
        const tr = document.createElement("tr");
        result.fields.forEach((field, index) => {
            const td = document.createElement("td");
            const value = row[field.name];
            if (value === null || value === undefined) {
                td.className = "wb-null";
                td.textContent = "null";
            } else {
                td.textContent = formats[index](value);
            }
            tr.append(td);
        });
        body.append(tr);
    }
    rowsText.textContent = result.total ? `rows ${first}-${last} of ${result.total}` : "no rows";
    prevButton.disabled = state.page === 0;
    nextButton.disabled = last >= result.total;
    statusText.textContent = `${result.ms} ms · ${result.total} row${result.total === 1 ? "" : "s"}`;
}

function sortBy(column) {
    const same = state.sort && state.sort.column === column;
    state.sort = {column, dir: same && state.sort.dir === "asc" ? "desc" : "asc"};
    state.page = 0;
    render(state);
}

function setMode(mode) {
    if (state.mode === mode) return;
    state.mode = mode;
    state.page = 0;
    render(state);
}

modeButtons.forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
fileSelect.addEventListener("change", async () => {
    state.active = fileSelect.value;
    state.sort = null;
    state.filter = "";
    state.page = 0;
    filterInput.value = "";
    el("[data-download]").href = byView.get(state.active).download_url;
    statusText.textContent = `loading ${byView.get(state.active).name}…`;
    await load(byView.get(state.active));
    if (state.mode === "sql") state.sql = (await compile(state)).query;
    render(state);
});
let filterTimer = null;
filterInput.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
        state.filter = filterInput.value.trim();
        state.page = 0;
        render(state);
    }, 250);
});
el("[data-run]").addEventListener("click", async () => {
    state.sql = sqlInput.value;
    state.page = 0;
    statusText.textContent = "waiting for every file to load…";
    await Promise.allSettled(files.map(load));
    render(state);
});
sqlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        el("[data-run]").click();
    }
});
sqlInput.addEventListener("input", () => { commandLine.textContent = command(sqlInput.value); });
prevButton.addEventListener("click", () => {
    state.page = Math.max(0, state.page - 1);
    if (state.mode === "sql") draw(); else render(state);
});
nextButton.addEventListener("click", () => {
    state.page += 1;
    if (state.mode === "sql") draw(); else render(state);
});

// One load per file, started for the open file first and for the rest in the
// background, so the page shows data as soon as its own file is in.
const loads = new Map();

function load(file) {
    if (!loads.has(file.view)) {
        loads.set(file.view, (async () => {
            const response = await fetch(file.url);
            if (!response.ok) throw new Error(`${file.name}: ${await response.text()}`);
            const name = `${file.view}.csv`;
            await db.registerFileBuffer(name, new Uint8Array(await response.arrayBuffer()));
            await conn.query(`CREATE VIEW ${ident(file.view)} AS SELECT * FROM read_csv_auto(${literal(name)})`);
            fileSelect.querySelector(`option[value="${file.view}"]`).textContent = file.name;
        })());
    }
    return loads.get(file.view);
}

async function loadOthers() {
    for (const file of files) {
        if (file.view === state.active) continue;
        fileSelect.querySelector(`option[value="${file.view}"]`).textContent = `${file.name} (loading…)`;
    }
    for (const file of files) {
        if (file.view !== state.active) await load(file).catch(() => {});
    }
}

async function start() {
    try {
        const duckdb = await import(root.dataset.module);
        db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING),
                                    new Worker(root.dataset.worker));
        await db.instantiate(root.dataset.wasm);
        conn = await db.connect();
        statusText.textContent = `loading ${byView.get(state.active).name}…`;
        await load(byView.get(state.active));
        await render(state);
        loadOthers();
    } catch (error) {
        statusText.textContent = "";
        errorBox.textContent = String(error.message || error);
        errorBox.hidden = false;
    }
}

start();
