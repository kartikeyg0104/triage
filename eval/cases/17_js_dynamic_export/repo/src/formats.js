const SEPARATORS = { Csv: ",", Tsv: "\t" };

for (const [suffix, separator] of Object.entries(SEPARATORS)) {
  module.exports["to" + suffix] = (rows) => rows.map((row) => row.join(separator)).join("\n");
}
