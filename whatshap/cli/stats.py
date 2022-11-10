"""
Print phasing statistics of a single VCF file
"""
import logging
from collections import defaultdict
from contextlib import ExitStack
import dataclasses
from statistics import median
from typing import List, Tuple, Optional, Dict, Sequence

from ..vcf import VcfReader, VcfVariant

logger = logging.getLogger(__name__)


# fmt: off
def add_arguments(parser):
    add = parser.add_argument
    add("--gtf", default=None, help="Write phased blocks to GTF file.")
    add("--sample", metavar="SAMPLE", help="Name of the sample "
        "to process. If not given, use first sample found in VCF.")
    add("--chr-lengths", metavar="FILE",
        help="Override chromosome lengths in VCF with those from FILE (one line per chromosome, "
        "tab separated '<chr> <length>'). Lengths are used to compute NG50 values.")
    add("--tsv", metavar="FILE", help="Write statistics in tab-separated value format to FILE")
    add("--only-snvs", default=False, action="store_true", help="Only process SNVs "
        "and ignore all other variants.")
    add("--block-list", metavar="FILE", help="Write list of all blocks to FILE (one block per line)")
    add("--chromosome", dest="chromosomes", metavar="CHROMOSOME", default=[], action="append",
        help="Name of chromosome to process. If not given, all chromosomes in the "
        "input VCF are considered. Can be used multiple times")
    add("vcf", metavar="VCF", help="Phased VCF file")
# fmt: on


def validate(args, parser):
    pass


class PhasedBlock:
    def __init__(self, chromosome=None):
        self.phases = {}
        self.leftmost_variant = None
        self.rightmost_variant = None
        self.chromosome = chromosome

    def add(self, variant, phase):
        if len(self.phases) == 0:
            self.leftmost_variant = variant
            self.rightmost_variant = variant
        else:
            if variant < self.leftmost_variant:
                self.leftmost_variant = variant
            if self.rightmost_variant < variant:
                self.rightmost_variant = variant
        self.phases[variant] = phase

    def span(self):
        """Returns the length of the covered genomic region in bp."""
        return self.rightmost_variant.position - self.leftmost_variant.position

    def variants(self):
        return list(sorted(self.phases.keys()))

    def count_snvs(self):
        return sum(int(variant.is_snv()) for variant in self.phases)

    def split(self, split_left: int, split_right: int) -> Tuple["PhasedBlock", "PhasedBlock"]:
        """Split this phaseblock in two, based on given positions. The first phaseblock will contain
        the variants to the left of split_left and the second the variants to the right of split_right."""
        assert split_left <= split_right
        left_block = PhasedBlock(chromosome=self.chromosome)
        right_block = PhasedBlock(chromosome=self.chromosome)
        for variant, phase in self.phases.items():
            if variant.position < split_left:
                left_block.add(variant, phase)
            elif variant.position > split_right:
                right_block.add(variant, phase)
        return left_block, right_block

    def __repr__(self):
        return f"PhasedBlock({str(self.phases)})"

    def __len__(self):
        return len(self.phases)

    def __lt__(self, other):
        return (self.leftmost_variant, self.rightmost_variant) < (
            other.leftmost_variant,
            other.rightmost_variant,
        )


class GtfWriter:
    def __init__(self, file):
        self._file = file

    def write(self, chromosome, start, stop, name):
        """
        Write a feature to the GTF. start is 0-based.
        """
        assert start < stop
        print(
            chromosome,
            "Phasing",
            "exon",
            start + 1,
            stop,
            ".",
            "+",
            ".",
            f'gene_id "{name}"; transcript_id "{name}.1";',
            sep="\t",
            file=self._file,
        )


@dataclasses.dataclass
class DetailedStats:
    variants: int
    phased: int
    unphased: int
    singletons: int
    blocks: int
    variant_per_block_median: float
    variant_per_block_avg: float
    variant_per_block_min: int
    variant_per_block_max: int
    variant_per_block_sum: int
    bp_per_block_median: float
    bp_per_block_avg: float
    bp_per_block_min: int
    bp_per_block_max: int
    bp_per_block_sum: int
    heterozygous_variants: int
    heterozygous_snvs: int
    phased_snvs: int
    block_n50: float

    def print(self, width: int = 21):
        # Parameters for value formatting
        max_integer_width = max(len(str(int(value))) for value in vars(self).values())
        value_width = max(max_integer_width, 8)
        format_int = f"{value_width}d"
        format_float = f"{value_width + 3}.2f"

        print("Variants in VCF:".rjust(width), f"{self.variants:{format_int}}")
        print(
            "Heterozygous:".rjust(width),
            f"{self.heterozygous_variants:{format_int}} ({self.heterozygous_snvs:{format_int}} SNVs)",
        )
        print(
            "Phased:".rjust(width),
            f"{self.phased:{format_int}} ({self.phased_snvs:{format_int}} SNVs)",
        )
        print("Unphased:".rjust(width), f"{self.unphased:{format_int}}", "(not considered below)")
        print(
            "Singletons:".rjust(width), f"{self.singletons:{format_int}}", "(not considered below)"
        )
        print("Blocks:".rjust(width), f"{self.blocks:{format_int}}")
        print()
        print("Block sizes (no. of variants)")
        print(
            "Median block size:".rjust(width),
            f"{self.variant_per_block_median:{format_float}} variants",
        )
        print(
            "Average block size:".rjust(width),
            f"{self.variant_per_block_avg:{format_float}} variants",
        )
        print(
            "Largest block:".rjust(width), f"{self.variant_per_block_max:{format_int}}    variants"
        )
        print(
            "Smallest block:".rjust(width),
            f"{self.variant_per_block_min:{format_int}}    variants",
        )
        print()
        print("Block lengths (basepairs)")
        print("Sum of lengths:".rjust(width), f"{self.bp_per_block_sum:{format_int}}    bp")
        print("Median block length:".rjust(width), f"{self.bp_per_block_median:{format_float}} bp")
        print("Average block length:".rjust(width), f"{self.bp_per_block_avg:{format_float}} bp")
        print("Longest block:".rjust(width), f"{self.bp_per_block_max:{format_int}}    bp")
        print("Shortest block:".rjust(width), f"{self.bp_per_block_min:{format_int}}    bp")
        print("Block NG50:".rjust(width), f"{self.block_n50:{format_int}}    bp")
        assert self.phased + self.unphased + self.singletons == self.heterozygous_variants


def n50(lengths: List[int], target_length: Optional[int] = None) -> int:
    if target_length is None:
        target_length = sum(lengths)

    lengths.sort(reverse=True)
    total = 0
    for length in lengths:
        total += length
        if total >= 0.5 * target_length:
            return length
    return 0


def compute_ng50(blocks, chr_lengths):
    chromosomes = {b.chromosome for b in blocks}
    target_length = 0
    for chromosome in sorted(chromosomes):
        try:
            target_length += chr_lengths[chromosome]
        except KeyError:
            logger.warning(
                "Not able to compute NG50 because length of contig '%s' not available", chromosome
            )
            return float("nan")

    block_lengths = [b.rightmost_variant.position - b.leftmost_variant.position for b in blocks]
    return n50(block_lengths, target_length=target_length)


class PhasingStats:
    def __init__(self):
        self.blocks = []
        self.split_blocks = []
        self.unphased = 0
        self.variants = 0
        self.heterozygous_variants = 0
        self.heterozygous_snvs = 0
        self.phased_snvs = 0

    def __iadd__(self, other):
        self.blocks.extend(other.blocks)
        self.split_blocks.extend(other.split_blocks)
        self.unphased += other.unphased
        self.variants += other.variants
        self.heterozygous_variants += other.heterozygous_variants
        self.heterozygous_snvs += other.heterozygous_snvs
        self.phased_snvs += other.phased_snvs
        return self

    def add_blocks(self, blocks: Sequence[PhasedBlock]):
        self.blocks.extend(blocks)
        self.split_blocks.extend(self.get_nonoverlapping_blocks())

    def add_unphased(self, unphased: int = 1):
        self.unphased += unphased

    def add_variants(self, variants: int):
        self.variants += variants

    def add_heterozygous_variants(self, variants: int):
        self.heterozygous_variants += variants

    def add_heterozygous_snvs(self, snvs: int):
        self.heterozygous_snvs += snvs

    def get_nonoverlapping_blocks(self) -> List[PhasedBlock]:
        """Split phase blocks into nonoverlapping subblocks"""
        pos_sorted_blocks = sorted(
            self.blocks, key=lambda b: (b.chromosome, b.leftmost_variant.position), reverse=True
        )

        # filter out blocks with only one variant
        pos_sorted_blocks = [b for b in pos_sorted_blocks if len(b) > 1]

        # iterate over blocks and split if overlapping until no blocks remain.
        split_blocks = []
        while pos_sorted_blocks:
            block = pos_sorted_blocks.pop()
            if pos_sorted_blocks:
                block_end = block.rightmost_variant.position
                next_block = pos_sorted_blocks[-1]
                next_block_start = next_block.leftmost_variant.position
                next_block_end = next_block.rightmost_variant.position

                # Check if next block overlapps current. If so split the current block.
                if (block_end > next_block_start) and (block.chromosome == next_block.chromosome):
                    block, new_block = block.split(next_block_start, next_block_end)

                    # Update sorting if right-side block is added.
                    if len(new_block) > 1:
                        pos_sorted_blocks.append(new_block)
                        pos_sorted_blocks = sorted(
                            pos_sorted_blocks,
                            key=lambda b: (b.chromosome, b.leftmost_variant.position),
                            reverse=True,
                        )

                    # Skip the left-side block if is it too short after splitting
                    if len(block) < 2:
                        continue
            split_blocks.append(block)

        return split_blocks

    def get_detailed_stats(self, chr_lengths: Optional[Dict[str, int]] = None) -> DetailedStats:
        """Return DetailedStats"""
        block_sizes = sorted(len(block) for block in self.blocks)
        n_singletons = sum(1 for size in block_sizes if size == 1)
        block_sizes = [size for size in block_sizes if size > 1]
        # Block length stats calculated from split interleaved blocks to avoid inflating values
        block_lengths = sorted(block.span() for block in self.split_blocks if len(block) > 1)
        phased_snvs = sum(block.count_snvs() for block in self.blocks if len(block) > 1)
        if block_sizes:
            return DetailedStats(
                variants=self.variants,
                phased=sum(block_sizes),
                unphased=self.unphased,
                singletons=n_singletons,
                blocks=len(block_sizes),
                variant_per_block_median=median(block_sizes),
                variant_per_block_avg=sum(block_sizes) / len(block_sizes),
                variant_per_block_min=block_sizes[0],
                variant_per_block_max=block_sizes[-1],
                variant_per_block_sum=sum(block_sizes),
                bp_per_block_median=median(block_lengths),
                bp_per_block_avg=sum(block_lengths) / len(block_lengths),
                bp_per_block_min=block_lengths[0],
                bp_per_block_max=block_lengths[-1],
                bp_per_block_sum=sum(block_lengths),
                heterozygous_variants=self.heterozygous_variants,
                heterozygous_snvs=self.heterozygous_snvs,
                phased_snvs=phased_snvs,
                block_n50=compute_ng50(self.split_blocks, chr_lengths)
                if chr_lengths is not None
                else float("nan"),
            )
        else:
            return DetailedStats(
                variants=self.variants,
                phased=0,
                unphased=self.unphased,
                singletons=n_singletons,
                blocks=0,
                variant_per_block_median=float("nan"),
                variant_per_block_avg=float("nan"),
                variant_per_block_min=0,
                variant_per_block_max=0,
                variant_per_block_sum=0,
                bp_per_block_median=float("nan"),
                bp_per_block_avg=float("nan"),
                bp_per_block_min=0,
                bp_per_block_max=0,
                bp_per_block_sum=0,
                heterozygous_variants=self.heterozygous_variants,
                heterozygous_snvs=self.heterozygous_snvs,
                phased_snvs=0,
                block_n50=float("nan"),
            )


def parse_chr_lengths(filename):
    chr_lengths = {}
    with open(filename) as f:
        for line in f:
            fields = line.split("\t")
            assert len(fields) == 2
            chr_lengths[fields[0]] = int(fields[1])
    return chr_lengths


def parse_variant_tables(vcf_reader, chromosomes=None):
    """
    Parse variant_tables from vcf_reader. If chromosomes are given and VCF is indexed,
    theses are accessed by direct lookup.
    """
    if chromosomes and vcf_reader.index_exists():
        for chromosome in chromosomes:
            yield vcf_reader.fetch(chromosome)
    else:
        yield from vcf_reader


def get_chr_lengths(chr_lengths_file, vcf_reader):
    if chr_lengths_file:
        chr_lengths = parse_chr_lengths(chr_lengths_file)
        logger.info("Read length of %d chromosomes from %s", len(chr_lengths), chr_lengths_file)
    else:
        chr_lengths = {
            chrom: contig.length
            for chrom, contig in vcf_reader.contigs.items()
            if contig.length is not None
        }
        if not chr_lengths:
            logger.warning(
                "VCF header does not contain contig lengths, cannot compute NG50. "
                "Consider using --chr-lengths"
            )
    return chr_lengths


def write_to_block_list(block_list_file, blocks, chromosome, sample):
    block_ids = sorted(blocks.keys())
    for block_id in block_ids:
        print(
            sample,
            chromosome,
            block_id,
            blocks[block_id].leftmost_variant.position + 1,
            blocks[block_id].rightmost_variant.position + 1,
            len(blocks[block_id]),
            sep="\t",
            file=block_list_file,
        )


@dataclasses.dataclass
class GtfBlock:
    start: int = 0
    end: int = 0
    id: str = None

    def add(self, variant: VcfVariant):
        self.end = variant.position + 1


def get_phase_blocks(chromosome, gtfwriter, sample, stats, variant_table):
    genotypes = variant_table.genotypes_of(sample)
    phases = variant_table.phases_of(sample)
    assert len(genotypes) == len(phases) == len(variant_table.variants)

    blocks = defaultdict(PhasedBlock)
    prev_block = GtfBlock()
    for variant, genotype, phase in zip(variant_table.variants, genotypes, phases):
        stats.add_variants(1)
        if genotype.is_homozygous():
            continue
        stats.add_heterozygous_variants(1)
        if variant.is_snv():
            stats.add_heterozygous_snvs(1)

        if phase is None:
            stats.add_unphased()
            continue

        blocks[phase.block_id].add(variant, phase)
        if gtfwriter:
            if prev_block.id is None:
                prev_block = GtfBlock(variant.position, variant.position + 1, phase.block_id)
            else:
                if prev_block.id != phase.block_id:
                    gtfwriter.write(chromosome, prev_block.start, prev_block.end, prev_block.id)
                    prev_block = GtfBlock(variant.position, variant.position + 1, phase.block_id)

                prev_block.add(variant)

    # Add chromosome information to each block. This is needed to
    # sort blocks later when we compute NG50s
    for block_id, block in blocks.items():
        block.chromosome = chromosome

    if gtfwriter and prev_block.id is not None:
        gtfwriter.write(chromosome, prev_block.start, prev_block.end, prev_block.id)

    return blocks


def run_stats(
    vcf,
    sample=None,
    gtf=None,
    tsv=None,
    block_list=None,
    only_snvs=False,
    chromosomes=None,
    chr_lengths=None,
):
    gtfwriter = tsv_file = block_list_file = None
    with ExitStack() as stack:
        if gtf:
            gtf_file = stack.enter_context(open(gtf, "wt"))
            gtfwriter = GtfWriter(gtf_file)

        vcf_reader = VcfReader(vcf, phases=True, indels=not only_snvs)
        if len(vcf_reader.samples) == 0:
            logger.error("Input VCF does not contain any sample")
            return 1
        else:
            logger.info(f"Found {len(vcf_reader.samples)} sample(s) in input VCF")
        if sample:
            if sample in vcf_reader.samples:
                sample = sample
            else:
                logger.error(f"Requested sample ({sample}) not found")
                return 1
        else:
            sample = vcf_reader.samples[0]
            logger.info(f"Reporting results for sample {sample}")

        chr_lengths = get_chr_lengths(chr_lengths, vcf_reader)

        if tsv:
            tsv_file = stack.enter_context(open(tsv, "w"))
            field_names = [f.name for f in dataclasses.fields(DetailedStats)]
            print("#sample", "chromosome", "file_name", *field_names, sep="\t", file=tsv_file)

        if block_list:
            block_list_file = stack.enter_context(open(block_list, "w"))
            print(
                "#sample",
                "chromosome",
                "phase_set",
                "from",
                "to",
                "variants",
                sep="\t",
                file=block_list_file,
            )

        print(f"Phasing statistics for sample {sample} from file {vcf}")
        total_stats = PhasingStats()
        chromosome_count = 0
        given_chromosomes = chromosomes
        seen_chromosomes = set()
        for variant_table in parse_variant_tables(vcf_reader, given_chromosomes):
            if given_chromosomes:
                seen_chromosomes.add(variant_table.chromosome)
                if variant_table.chromosome not in given_chromosomes:
                    continue
            chromosome_count += 1
            chromosome = variant_table.chromosome
            stats = PhasingStats()
            print(f"---------------- Chromosome {chromosome} ----------------")
            blocks = get_phase_blocks(chromosome, gtfwriter, sample, stats, variant_table)

            if block_list_file:
                write_to_block_list(block_list_file, blocks, chromosome, sample)

            stats.add_blocks(blocks.values())

            detailed_stats = stats.get_detailed_stats(chr_lengths)
            detailed_stats.print()
            if tsv_file:
                print(sample, chromosome, vcf, sep="\t", end="\t", file=tsv_file)
                print(*dataclasses.astuple(detailed_stats), sep="\t", file=tsv_file)

            total_stats += stats

            if given_chromosomes and set(given_chromosomes) <= seen_chromosomes:
                break

        if chromosome_count > 1:
            print("---------------- ALL chromosomes (aggregated) ----------------")
            detailed_stats = total_stats.get_detailed_stats(chr_lengths)
            detailed_stats.print()
            if tsv_file:
                print(sample, "ALL", vcf, sep="\t", end="\t", file=tsv_file)
                print(*dataclasses.astuple(detailed_stats), sep="\t", file=tsv_file)


def main(args):
    run_stats(**vars(args))
