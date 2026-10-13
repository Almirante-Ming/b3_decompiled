import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;

import java.io.BufferedReader;
import java.io.FileReader;

/**
 * Applies XbSymbolDatabase matches (see tools/signatures/) to the current
 * program: renames functions and labels variables/constants at their
 * resolved addresses, tagging each with its source library.
 *
 * Input: a TSV produced by tools/signatures/parse_matches.py --tsv, with
 * columns: address(hex) \t library \t kind(FUN|VAR) \t name
 *
 * Entries below the program's image base are struct-member offsets (e.g.
 * "..._OFFSET" symbols relative to a runtime object), not addresses, and
 * are skipped on purpose.
 *
 * Usage: analyzeHeadless ... -postScript ApplyLibraryMatches.java <tsv_path>
 */
public class ApplyLibraryMatches extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            println("RESULT_ERROR=missing tsv path argument");
            return;
        }
        String tsvPath = args[0];

        Address imageBase = currentProgram.getImageBase();
        SymbolTable st = currentProgram.getSymbolTable();

        int renamedFunctions = 0;
        int labeledVars = 0;
        int skippedOffset = 0;
        int skippedNoFunction = 0;
        int errors = 0;
        int total = 0;

        BufferedReader reader = new BufferedReader(new FileReader(tsvPath));
        String line;
        while ((line = reader.readLine()) != null) {
            if (line.isEmpty()) continue;
            String[] cols = line.split("\t", 4);
            if (cols.length != 4) continue;
            total++;

            try {
                long addrVal = Long.decode(cols[0]);
                String library = cols[1];
                String kind = cols[2];
                String name = cols[3];

                Address addr = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(addrVal);
                if (addr.compareTo(imageBase) < 0) {
                    skippedOffset++;
                    continue;
                }

                String safeName = name.replaceAll("[^A-Za-z0-9_]", "_");
                String comment = "XbSymbolDatabase: " + library + " " + kind;

                if (kind.equals("FUN")) {
                    Function f = getFunctionAt(addr);
                    if (f == null) {
                        skippedNoFunction++;
                        continue;
                    }
                    f.setName(safeName, SourceType.IMPORTED);
                    f.setComment(comment);
                    renamedFunctions++;
                } else {
                    Symbol existing = getSymbolAt(addr);
                    if (existing != null && existing.getSource() != SourceType.DEFAULT) {
                        // Already has a real name (e.g. a function's symbol); don't clobber it.
                        continue;
                    }
                    st.createLabel(addr, safeName, SourceType.IMPORTED);
                    setEOLComment(addr, comment);
                    labeledVars++;
                }
            } catch (Exception e) {
                errors++;
            }
        }
        reader.close();

        println("RESULT_TOTAL_LINES=" + total);
        println("RESULT_RENAMED_FUNCTIONS=" + renamedFunctions);
        println("RESULT_LABELED_VARS=" + labeledVars);
        println("RESULT_SKIPPED_OFFSET=" + skippedOffset);
        println("RESULT_SKIPPED_NO_FUNCTION=" + skippedNoFunction);
        println("RESULT_ERRORS=" + errors);
    }
}
