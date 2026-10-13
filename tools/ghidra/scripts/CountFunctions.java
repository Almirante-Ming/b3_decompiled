import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.SymbolTable;
import ghidra.program.model.mem.MemoryBlock;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * Quick scale-of-work metrics for a freshly analyzed binary: how many
 * functions exist, how many already carry a non-default name (library
 * matches / Function ID hits), and how much disassembled code there is.
 */
public class CountFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        FunctionManager fm = currentProgram.getFunctionManager();
        Listing listing = currentProgram.getListing();
        SymbolTable st = currentProgram.getSymbolTable();

        int total = fm.getFunctionCount();
        int thunks = 0;
        int namedNonDefault = 0;
        List<String> sample = new ArrayList<String>();

        Iterator<Function> it = fm.getFunctions(true);
        int i = 0;
        while (it.hasNext()) {
            Function f = it.next();
            if (f.isThunk()) thunks++;
            if (f.getSymbol().getSource() != SourceType.DEFAULT) namedNonDefault++;
            if (i < 25) {
                sample.add(f.getName() + "@" + f.getEntryPoint());
            }
            i++;
        }

        println("RESULT_FUNCTION_COUNT=" + total);
        println("RESULT_THUNK_COUNT=" + thunks);
        println("RESULT_NAMED_NONDEFAULT_COUNT=" + namedNonDefault);
        println("RESULT_INSTRUCTION_COUNT=" + listing.getNumInstructions());
        println("RESULT_DEFINED_DATA_COUNT=" + listing.getNumDefinedData());
        println("RESULT_SYMBOL_COUNT=" + st.getNumSymbols());

        MemoryBlock[] blocks = currentProgram.getMemory().getBlocks();
        println("RESULT_MEMORY_BLOCKS=" + blocks.length);

        StringBuilder sb = new StringBuilder();
        for (String s : sample) {
            if (sb.length() > 0) sb.append(",");
            sb.append(s);
        }
        println("RESULT_SAMPLE_FUNCS=" + sb.toString());
    }
}
