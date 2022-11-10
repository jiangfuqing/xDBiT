  #!/usr/bin/env python

'''
Script to run xDbit pipeline on multiple batches.

Usage:

    Idea:
    Input is a .csv file giving the parameters:
        - directory of fastqs
        - STAR genome directory
        - STAR genome fasta file path
        - directory of barcode legend .csv
        - number of expected spots
        - mode

Command for single analysis:
nohup bash /home/jwirth/projects/xDbit_toolbox/xDbit_pipeline.sh -g /home/hpc/meier/genomes_STAR/mm_STARmeta/STAR/ 
-r /home/hpc/meier/genomes_STAR/mm_STARmeta/Mm_metadata.fasta -b ../barcodes/barcodes_legend.csv -n 2000 -m Dbit-seq -j 1 
../37_30_A2_S2_R1_001.fastq ../37_30_A2_S2_R2_001.fastq &

'''

from subprocess import Popen, PIPE, STDOUT
from os.path import dirname, join
import pandas as pd
import sys
from datetime import datetime
import time
import numpy as np
import argparse

print("Starting xDbit pipeline batch processing...", flush=True)
## Start timing
t_start = datetime.now()

# Parse arguments
print("Parse arguments")
parser = argparse.ArgumentParser()
parser.add_argument("-f", "--batch_file", help="Batch file with all parameters for xDbit batch processing.")
parser.add_argument("-s", "--skip_align", action='store_true', help="Skip alignment steps for testing.")
parser.add_argument("-c", "--clear_tmp", help="Clear files in temporary directory after pipeline is finished.")
parser.add_argument("-e", "--echo", help="Print all commands to output instead of executing them.")
args = parser.parse_args()

print("Reading batch parameters from {}".format(args.batch_file))
settings = pd.read_csv(args.batch_file)

batch_numbers = settings['batch'].unique()
print("Found following batch numbers: {}".format(batch_numbers))

for b in batch_numbers:
    batch = settings.query('batch == {}'.format(b))

    print("{} : Start processing batch {} of {} with {} files".format(f"{datetime.now():%Y-%m-%d %H:%M:%S}", b, batch_numbers[-1], len(batch)), flush=True)
    ## Start timing of batch
    t_start_batch = datetime.now()

    ## Generate Commands
    commands = []
    log_dirs = []
    for i in range(len(batch)):
        # extract settings
        s = batch.iloc[i, :]

        # get directory
        log_dir = join(dirname(s["fastq_R1"]), "pipeline_batch{}_{}.out".format(b, f"{datetime.now():%Y_%m_%d}"))
        out_dir = join(dirname(s["fastq_R1"]), "out")
        tmp_dir = join(dirname(s["fastq_R1"]), "tmp")

        print("\tWriting log to {}".format(log_dir), flush=True)
        print("\tOutput directory: {}".format(out_dir), flush=True)
        print("\tTemp directory: {}".format(tmp_dir), flush=True)

        # generate commands
        commands.append(["bash", s["pipeline_dir"], 
        "-g", s["STAR_genome"],
        "-r", s["STAR_fasta"],
        "-b", s["legend"],
        "-n", str(s["expected_n_spots"]),
        "-m", s["mode"],
        "-j", "1",
        "-o", out_dir,
        "-t", tmp_dir,
        #"-e", # for testing
        ])

        # add other options
        if args.skip_align:
            commands[i].append("-x")
        if args.clear_tmp:
            commands[i].append("-l")
        if args.echo:
            commands[i].append("-e")

        # add files to command
        commands = [commands[i] + [s["fastq_R1"], s["fastq_R2"]]]

        log_dirs.append(log_dir)

    procs = [Popen(commands[i], stdout=PIPE, stderr=STDOUT) for i in range(len(commands))]

    running = [True]*len(procs)

    # open log files
    log_files = [open(d, "a") for d in log_dirs]

    while True:
        for i, p in enumerate(procs):
            output = p.stdout.readline()
            if running[i] and p.poll() is not None:
                running[i] = False
                print("{}: Process {} in batch {} finished".format(f"{datetime.now():%Y-%m-%d %H:%M:%S}", i+1, b), flush=True)
            if output:
                print(output.decode("utf-8").strip(), file=log_files[i], flush=True)
        if not np.any(running):
            ## Stop timing
            t_stop_batch = datetime.now()
            t_elapsed_batch = t_stop_batch-t_start_batch
            print("{}: All processes of batch {} finished after {}.".format(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}", b, str(t_elapsed_batch)), flush=True)
            break

    # close log files
    for f in log_files:
        f.close()

## Stop timing
t_stop = datetime.now()
t_elapsed = t_stop-t_start
    
print("{}: All batches finished after {}.".format(f"{datetime.now():%Y-%m-%d %H:%M:%S}", str(t_elapsed)), flush=True)
