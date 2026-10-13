import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.symbol.SourceType;

import java.io.FileWriter;
import java.util.Iterator;

/**
 * Dumps every function's (start, end, current-name, is-default-name) to a
 * TSV, so external tools (e.g. a standalone FLIRT matcher) can work from the
 * same function boundaries Ghidra already found, without needing to run
 * inside Ghidra themselves.
 *
 * Usage: analyzeHeadless ... -postScript ExportFunctionAddresses.java <out_tsv_path>
 */
public class ExportFunctionAddresses extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            println("RESULT_ERROR=missing output path argument");
            return;
        }
        String outPath = args[0];

        FunctionManager fm = currentProgram.getFunctionManager();
        FileWriter fw = new FileWriter(outPath);

        int count = 0;
        Iterator<Function> it = fm.getFunctions(true);
        while (it.hasNext()) {
            Function f = it.next();
            long start = f.getEntryPoint().getOffset();
            long end = start;
            for (var range : f.getBody()) {
                long maxAddr = range.getMaxAddress().getOffset();
                if (maxAddr > end) end = maxAddr;
            }
            boolean isDefault = f.getSymbol().getSource() == SourceType.DEFAULT;
            fw.write(String.format("0x%08x\t0x%08x\t%s\t%s\n", start, end, isDefault ? "1" : "0", f.getName()));
            count++;
        }
        fw.close();
        println("RESULT_EXPORTED_FUNCTIONS=" + count);
    }
}
