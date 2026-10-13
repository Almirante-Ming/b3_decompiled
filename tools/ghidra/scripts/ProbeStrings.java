import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.util.DefinedDataIterator;
import ghidra.program.model.data.StringDataInstance;

public class ProbeStrings extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] needles = {
            "RenderWare", "RWCORE", "Criterion", ".pdb", ".cpp", ".c\"",
            "Microsoft (R)", "Visual C++", "XDK", "EA Black Box", "Blackbox",
            "Burnout", "GCC", "assert", "ASSERT", "D:\\\\", "C:\\\\",
            ".obj", "RWS", "rwc", "__FILE__"
        };
        int[] counts = new int[needles.length];
        int totalStrings = 0;
        java.util.List<String> hits = new java.util.ArrayList<String>();

        DataIterator it = currentProgram.getListing().getDefinedData(true);
        while (it.hasNext()) {
            Data d = it.next();
            if (!StringDataInstance.isString(d)) continue;
            totalStrings++;
            String val;
            try {
                val = d.getDefaultValueRepresentation();
            } catch (Exception e) { continue; }
            if (val == null) continue;
            for (int i = 0; i < needles.length; i++) {
                if (val.contains(needles[i])) {
                    counts[i]++;
                    if (hits.size() < 60) {
                        hits.add(needles[i] + " :: " + d.getAddress() + " :: " + val);
                    }
                }
            }
        }

        println("RESULT_TOTAL_STRINGS=" + totalStrings);
        for (int i = 0; i < needles.length; i++) {
            println("RESULT_NEEDLE[" + needles[i] + "]=" + counts[i]);
        }
        println("RESULT_HITS_BEGIN");
        for (String h : hits) println("HIT:: " + h);
        println("RESULT_HITS_END");
    }
}
