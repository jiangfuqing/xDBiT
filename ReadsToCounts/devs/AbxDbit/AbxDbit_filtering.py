#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
This is to process AbxDbit reads after tagging the bam file with cellular and molecular barcodes.

Input: BAM file with reads that have been filtered, polyA and SMART adapter trimmed, and tagged with the following:
    - XX: x-coordinate
    - XY: y-coordinate
    - (OPTIONAL) XZ: z-coordinate (well coordinate in xDbit)
    - XM: molecular barcode (UMI)

Algorithm:
    - Choice between levenshtein distance and hamming distance for correction

Output:
    - Filtered bam file containing only reads with expected barcodes. Barcodes that are within 1 hamming distance of an expected
    barcode sequence are corrected. An additional tag XC is added, which consists of the concatenated coordinates separated by an 'x' (XxYxZ).


Copyright: Johannes Wirth, Meier Lab, Helmholtz Pioneer Campus, Helmholtz Zentrum Munich, 2021


The code is based on work with a Copyright by Rebekka Wegmann (Snijderlab, ETH Zurich, 2019) which has been published in following GitHub repository: 
https://github.com/RebekkaWegmann/splitseq_toolbox

# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
    
"""

## libraries
import sys, os, h5py
import pysam
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from argparse import ArgumentParser
from datetime import datetime, timedelta
import subprocess
import glob
import collections
from multiprocessing import Pool
import Levenshtein as lv
import json
import gzip
import string
from itertools import combinations


## Functions

def well2ind(well):
    """Convert well positions to (zero-based) indices"""
    d = {'A':0,'B':1,'C':2,'D':3,'E':4,'F':5,'G':6,'H':7}
    row = d[well[0]]
    col = int(well[1:])-1
    return [row,col]

def make_plate_overview(well_count):
    # bring the counts per well into a plate layout
    out = np.zeros([8, 12])
    for wellpos in well_count.items():
        xy = well2ind(wellpos[0])
        out[xy[0], xy[1]] = int(wellpos[1])
    return out

def plot_plate_overview(bc_matrices, all_bc_cumsum, coord_names, est_num_cells, out_dir):
    ## plotting summary graphs
    fig, axs = plt.subplots(2,2)
    axs = axs.ravel()

    for i, name in enumerate(coord_names):
        # plot heat map
        matrix = bc_matrices[name]
        p = axs[i].imshow(np.log10(matrix+1))
        
        # set title
        axs[i].set_title('Number of reads per {}-coordinate'.format(name))
        
        # set x and y ticks
        axs[i].set_xticks(list(range(0, 12)), list(range(1, 13)))
        axs[i].set_yticks(list(range(0, 8)), list(string.ascii_uppercase[:8]))
        axs[i].tick_params(axis='both', which='both',length=0)
        
        # set color bar
        clb = fig.colorbar(p, ax=axs[i])
        clb.set_label('No. BCs, log10')

    # plotting the cumulative fraction of reads per barcode helps to determine the number of assayed cells (see Macosko, 2015)
    axs[3].plot(all_bc_cumsum); axs[3].set_title('Cumulative fraction of reads per barcode')
    axs[3].set_xlim(0, est_num_cells * 1.2)
    axs[3].set_xlabel("Barcodes sorted by number of reads")
    axs[3].set_ylabel("Cumulative fraction of reads")

    fig.set_size_inches(12,7)
    fig.savefig(os.path.join(out_dir,'filtering_summary.pdf'),bbox_inches='tight')

def average_time(time_list):
    average_timedelta = sum(time_list, timedelta(0)) / len(time_list)
    return average_timedelta

def get_stdout(command):
    process = subprocess.Popen(command,stdout=subprocess.PIPE, shell=True)
    proc_stdout = process.communicate()[0].strip()
    return proc_stdout

def sum_dicts(dictlist):
    counter = collections.Counter() 
    for d in dictlist:  
        counter.update(d) 
      
    result = dict(counter)
    return result

def create_barcode_dict(barcode_legend, coordinate_name):
    '''
    Function to generate barcode dictionary.
    Expects following inputs:
        1. 'barcode_legend': data from .csv file with columns: [WellPosition, Name, Barcode, X, Y, Z]
        2. 'coordinate_name': name of coordinate.
    '''
    d = {barcode: [well[0] + well[1:].zfill(2), int(coord)] for (barcode, well, coord) in zip(barcode_legend["Barcode"],
                                                          barcode_legend["WellPosition"],
                                                          barcode_legend[coordinate_name]) if not pd.isnull(coord)}
    return d

def check_barcode_and_add_coord_to_tag(entry, barcodes, coord_name, barcode_dictionaries, 
    record_dictionary, dist_threshold, compute_dist_function, keep):

    '''
    Function to check a barcode against a list and add a coordinate tag to a pysam entry.
    Procedure:
        1. Check barcode against list.
        2. If it does not match exactly a distance computation function is applied.
        3. If unique match is found the barcode is transformed to the spatial coordinate using the barcode dictionary.
        4. Pysam entry is tagged with coordinate.
        5. If no match is found the keep variable is set to False.
    '''
    
    barcode = barcodes[coord_name]
    barcode_dict = barcode_dictionaries[coord_name] # lookup in dictionaries is faster
    barcode_list = list(barcode_dict.keys()) # iteration through lists is slightly faster

    if barcode in barcode_dict:
        # Not all barcodes were found directly but bc1
        # Determine well coordinate and set new tag with well coordinate
        well = barcode_dict[barcode][0]
        coord = barcode_dict[barcode][1]

        entry.set_tag("X" + coord_name, str(coord))

        # record
        record_dictionary[coord_name]['direct']+=1
        record_dictionary[coord_name]['well_counts'][well]+=1

    else:
        # Barcode 1 was not found directly. Barcode with certain distance is searched.
        d = [compute_dist_function(barcode,bc) for bc in barcode_list]
        idx = [i for i,e in enumerate(d) if e <= dist_threshold]

        if len(idx)==1:
            barcode = barcode_list[idx[0]]

            well = barcode_dict[barcode][0]
            coord = barcode_dict[barcode][1]

            entry.set_tag("X" + coord_name, str(coord))

            # record
            record_dictionary[coord_name]['corrected']+=1
            record_dictionary[coord_name]['well_counts'][well]+=1

        else:
            keep = False
            coord = None
            well = None

    return entry, record_dictionary, coord, well, keep

def spatialfilter(in_bam):
    # count read length of input bam
    print("Count number of reads in input bam...", flush=True)
    bam_length = get_stdout('samtools view ' + in_bam + ' | wc -l')
    total_reads = int(bam_length.decode())

    # read input bam
    infile = pysam.AlignmentFile(in_bam, 'rb', check_sq=False)
    filename = in_bam.split('/')[-1]

    # generate output bam name and directory
    if multi:
        out_bam = '/'.join(in_bam.split('/')[:-1]) + '/out_' + in_bam.split('/')[-1]
    else:
        out_bam = os.path.join(tmp_dir, "unaligned_tagged_BC_filtered.bam")

    outfile = pysam.AlignmentFile(out_bam, 'wb', template=infile)

    # setup recording dictionary
    record_dict = {
    'total_count': 0, 
    'total_count_kept': 0,
    'all_direct': 0
    }

    for name in coord_names:
        record_dict[name] = {}
        record_dict[name]['direct'] = 0
        record_dict[name]['corrected'] = 0
        record_dict[name]['well_counts'] = {e[0][0]+e[0][1:].zfill(2):0 for e in barcode_dicts[name].values()}

    # set up list to collect the barcodes for later statistics
    all_bcs =  [None]*est_num_cells*100

    if feat_file is not None:
        # add features category to record dictionary
        record_dict['features'] = {}
        record_dict['features']['direct'] = 0
        record_dict['features']['corrected'] = 0
        record_dict['features']['spot_found'] = 0
        record_dict['features']['umi_found'] = 0
        record_dict['features']['added'] = 0
        
        for v in feature_legend.Feature.values:
            record_dict['features'][v] = 0
        
        # for IntAct-seq
        record_dict['interact'] = {}
        record_dict['interact']['direct'] = 0
        record_dict['interact']['corrected'] = 0
        record_dict['interact']['spot_found'] = 0
        record_dict['interact']['umi_found'] = 0
        record_dict['interact']['added'] = 0
        
        interaction_combinations = ["+".join(map(str, comb)) for comb in combinations(feature_legend.Feature.values, 2)]
        for c in interaction_combinations:
            record_dict['interact'][c] = 0
        
    # start timing
    start_time_filtering = datetime.now()
    start_time = datetime.now()

    # start filtering
    print("Filtering started...", flush=True)

    for entry in infile.fetch(until_eof=True):
        record_dict['total_count']+=1
        keep = True
        add = True
        interact_signal = False

        # extract barcodes from entry
        barcodes = {name: entry.get_tag("X" + name) for name in coord_names}

        # check if all barcodes are in the respective barcode sets
        if np.all([barcodes[name] in barcode_dicts[name] for name in coord_names]):
            # if all barcodes directly found:
            # get well coordinate
            wells = {name: barcode_dicts[name][bc][0] for (name, bc) in barcodes.items()}

            # get coordinate
            coords = {name: barcode_dicts[name][bc][1] for (name, bc) in barcodes.items()}

            # save coordinate as tag
            for name in coord_names:
                entry.set_tag('X' + name, str(coords[name]))


            # record findings
            for name in coord_names:
                record_dict[name]['direct']+=1

                well = wells[name]
                record_dict[name]['well_counts'][well]+=1

            record_dict['all_direct']+=1            

        else:
            coords = {}
            wells = {}
            for name in coord_names:
                
                # check barcode and add coordinate to tag if matching barcode found.
                entry, record_dict, coord, well, keep = check_barcode_and_add_coord_to_tag(
                    entry=entry, barcodes=barcodes, coord_name=name, 
                    barcode_dictionaries=barcode_dicts, record_dictionary=record_dict, 
                    dist_threshold=dist_thresholds[name], compute_dist_function=compute_dist,
                    keep=keep)

                # collect the coordinates
                coords[name] = coord
                wells[name] = well

        if keep:
            # concatenate coordinates
            coords_list = [str(coords[name]) for name in coord_names]
            coord_concat = "x".join(coords_list)

            # check if features have to be extracted too
            if feat_file is not None:
                xg = entry.get_tag('XG')
                
                # check if there is an IntAct read in the entry
                if entry.has_tag('XH'):
                    xh = entry.get_tag('XH')
                    interact_signal = True
                else:
                    xh = None
                
                umi = entry.get_tag('XM')

                # check first possible feature tag
                if xg in feature_dict:
                    # get feature name
                    featurename = feature_dict[xg]
                    entry.set_tag('gn', featurename, value_type='Z')

                    # record
                    record_dict['features']['direct']+=1
                    record_dict['features'][featurename]+=1

                else:
                    # If barcode is not found in feature barcode list: Check for mismatches in feature barcode
                    d = [compute_dist(xg,bc) for bc in feature_barcodes]
                    idx = [i for i,e in enumerate(d) if e<=feature_dist]

                    if len(idx)==1:
                        xg = feature_barcodes[idx[0]]
                        featurename = feature_dict[xg]
                        entry.set_tag('gn', featurename, value_type = 'Z')

                        # record
                        record_dict['features']['corrected']+=1
                        record_dict['features'][featurename]+=1
                    else:
                        keep=False
                
                if keep and interact_signal:
                    # check first possible feature tag
                    if xh in feature_dict:
                        # add feature name to first featurename to mark an interact signal
                        featurename = featurename + "+" + feature_dict[xh]
                        entry.set_tag('gn', featurename, value_type='Z')

                        # record
                        record_dict['interact']['direct']+=1
                        
                        if featurename in record_dict['interact'].keys():
                            record_dict['interact'][featurename]+=1
                        else:
                            record_dict['interact'][featurename]=1

                    else:
                        # If barcode is not found in feature barcode list: Check for mismatches in feature barcode
                        d = [compute_dist(xg,bc) for bc in feature_barcodes]
                        idx = [i for i,e in enumerate(d) if e<=feature_dist]

                        if len(idx)==1:
                            xg = feature_barcodes[idx[0]]
                            featurename = feature_dict[xg]
                            entry.set_tag('gn', featurename, value_type = 'Z')

                            # record
                            record_dict['interact']['corrected']+=1
                            
                            if featurename in record_dict['interact'].keys():
                                record_dict['interact'][featurename]+=1
                            else:
                                record_dict['interact'][featurename]=1
                            
                        else:
                            keep=False
                    

        if keep:
            # set coordinates as tag and write to output file
            entry.set_tag('XC', str(coord_concat))
            outfile.write(entry)

            # record statistics
            if record_dict['total_count_kept'] < est_num_cells * 100 / ncores:
                all_bcs[record_dict['total_count_kept']] = coord_concat
            record_dict['total_count_kept']+=1

            if feat_file is not None:
                if coord_concat in rna_spot_list:
                    current_umi_list = umi_dict[coord_concat][featurename]

                    # record
                    if interact_signal:
                        record_dict['interact']['spot_found']+=1
                    else:
                        record_dict['features']['spot_found']+=1
                    
                    if featurename in total_umi_dict[coord_concat].keys():
                        total_umi_dict[coord_concat][featurename].append(umi)
                    else:
                        total_umi_dict[coord_concat][featurename] = [umi]

                    # Check if UMI was already used for this spot
                    if umi in current_umi_list:
                        # if UMI of current read was found in the UMI list of the respective spot: Discard read.
                        add = False

                        # record
                        if interact_signal:
                            record_dict['interact']['umi_found']+=1
                        else:
                            record_dict['features']['umi_found']+=1
                    
                else:
                    add = False
                
                if add:
                    # If cell barcode in RNA cell list and UMI was not used before: Count +1 in DGE.
                    if featurename in dge.index:
                        dge.loc[featurename, coord_concat]+=1
                    else:
                        # if interaction featurename is not yet in dge dataframe add it here and add count
                        dge.loc[featurename] = [0] * dge.shape[1]
                        dge.loc[featurename, coord_concat]+=1
                    # And add UMI to UMI list
                    if featurename in umi_dict[coord_concat].keys():
                        umi_dict[coord_concat][featurename].append(umi)
                    else:
                        umi_dict[coord_concat][featurename] = [umi]

                    # record
                    if interact_signal:
                        record_dict['interact']['added']+=1
                    else:
                        record_dict['features']['added']+=1

        elif store_discarded:
                print(entry.query_name, file = open(os.path.join(out_dir,'discarded_reads.txt'),'a'), flush=True)

        total_count = record_dict['total_count']
        if total_count % stride == 0:
            totaltime = datetime.now() - start_time_filtering
            stride_steptime = datetime.now() - start_time
            time_to_go = (total_reads - total_count) / stride * stride_steptime
            print("File " + filename + " - Reads " + str(total_count) + "/" + str(total_reads) + " processed. Time for last " + str(stride) + ": " + str(stride_steptime) + ", Total time: " + str(totaltime) + ". Time remaining: " + str(time_to_go),
                flush=True)
            start_time = datetime.now()
            
    all_bcs = all_bcs[:record_dict['total_count_kept']]
    n_all_bcs = len(all_bcs)
    print("Filtering finished.", flush=True)
    
    infile.close()
    outfile.close()
    #return [n, [n_wellx, n_welly], all_bcs, n_all_bcs]
    return [record_dict, all_bcs, n_all_bcs]


if __name__ == '__main__':
    ## Run Script
    
    # Setup input parser
    parser = ArgumentParser()
    parser.add_argument("-i" "--input_bam", action="store", dest="input_bam", default="-", help="Specify the input bam file. Defaults to stdin.")
    parser.add_argument("-n" "--est_num_cells", action="store", dest="est_num_cells", default=2500, help="Estimated number of cells. Defaults to 2500.",type=int)
    parser.add_argument("-d" "--out_dir", action="store", dest="out_dir", default=".", help="Directory to store logfiles and output plots. Defaults to the current directory.")
    parser.add_argument("-t" "--tmp_dir", action="store", dest="tmp_dir", default=".", help="Temp directory")
    parser.add_argument("-b" "--bc_file", action="store", dest="bc_file", default=None, help="Path to the barcode legend. Defaults to a file barcodes_legend.csv in the current directory.")
    parser.add_argument("--debug_flag",action="store_true",help="Turn on debug flag. This will produce some additional output which might be helpful.")
    parser.add_argument("--store_discarded",action="store_true",help="Store names of discarded reads?")
    parser.add_argument("-m", action='store_true', help="Use multithreading?")
    parser.add_argument("--mode", action='store', default='xDbit', help="Which mode? xDbit or Dbit-seq?")
    parser.add_argument("-f" "--feature_file", action="store", dest="feature_file", default=None, help="Path to the feature legend. Defaults to a file feature_legend.csv in the current directory.")
    parser.add_argument("-r" "--rna_dge_file", action='store', dest="rna_dge_file", default='-', help="Specify RNA DGE matrix")
    parser.add_argument("--interact", action='store_true', help="Use flag if there are interaction reads in the data.")


    ## Start timing
    t_start = datetime.now()

    ## Parse input
    args = parser.parse_args()

    debug_flag = args.debug_flag
    store_discarded = args.store_discarded
    input_bam = args.input_bam #use "-" for stdin, set flag to rb
    est_num_cells = args.est_num_cells
    out_dir = args.out_dir
    tmp_dir = args.tmp_dir
    bc_file = args.bc_file
    multi = args.m
    mode = args.mode
    feat_file = args.feature_file
    rna_dge_file = args.rna_dge_file
    interact_mode = args.interact
    
    feature_dist = 2
    

    split_dir = os.path.join(tmp_dir, 'tmp_split')

    # define frequency of printed outputs during barcode filtering
    stride = 500000

    # Fetch flags
    #if bc_dir==".":
    #    bc_dir = os.path.abspath(os.path.dirname(sys.argv[0]))

    # check out mode
    if mode == 'Dbit-seq':
        coord_names = ['X', 'Y']
    elif mode == 'xDbit':
        coord_names = ['X', 'Y', 'Z']
    else:
        # exit script
        sys.exit('{} is no valid mode ["xDbit", "Dbit-seq"]'.format(mode))
        
    ## Import barcode legend and extract information
    barcode_legend = pd.read_csv(bc_file)

    # create barcode dictionary
    barcode_dicts = {name: create_barcode_dict(barcode_legend, name) for name in coord_names}
    # retrieve string matching settings
    dist_alg = barcode_legend['string_matching_algorithm'][0]
    dist_thresholds = {name: int(barcode_legend[name + "_maxdist"][0]) for name in coord_names}
    
    # check which distance algorithm to use
    if dist_alg == "hamming":
        compute_dist = lv.hamming
    elif dist_alg == "levenshtein":
        compute_dist = lv.distance
    else:
        print("Unknown string matching alogrithm. Used levenshtein as default.", flush=True)
        compute_dist = lv.distance

    # Check if feature legend file is given
    if feat_file is not None:
        # prepare everything for feature extraction if necessary
        # import feature legend
        feature_legend = pd.read_csv(feat_file)

        # create feature barcode dict
        feature_dict = {feature_legend.Barcode.values[x]:feature_legend.Feature.values[x] for x in range(len(feature_legend))}
        feature_barcodes = feature_legend.Barcode.values

        # read RNA cell list from RNA DGE matrix.
        if rna_dge_file.endswith('.txt.gz'):
            with gzip.open(rna_dge_file, 'rt') as f:
                first_line = f.readline()
                rna_spot_list = first_line.rstrip('\n').split('\t')[1:]
        else:
            if rna_dge_file.endswith('.txt'):
                with open(rna_dge_file, 'rt') as f:
                    first_line = f.readline()
                    rna_spot_list = first_line.rstrip('\n').split('\t')[1:]
            else:
                print('Wrong file format for RNA cell (Neither .txt not .txt.gz)', flush=True)

        # create dictionary to collect all UMIs per cell
        umi_dict = {cell:{gene:[] for gene in feature_legend.Feature.values} for cell in rna_spot_list}
        total_umi_dict = {cell:{gene:[] for gene in feature_legend.Feature.values} for cell in rna_spot_list}
        
        if interact_mode:
            # add also all combinations of antibodies as entries in umi dictionaries
            for dic in [umi_dict, total_umi_dict]:
                for cell in dic:
                    for v in feature_legend.Feature.values:
                        for w in feature_legend.Feature.values:
                            dic[cell][v + "+" + w] = []

        # create empty gene expression matrix
        dge = pd.DataFrame(0, index=feature_legend.Feature, columns=rna_spot_list, dtype=int)

    ## Write parameters to logfile
    print('AbxDbit barcode filtering log - based on %s algorithm\n---------------------------------------\nParameters:' % dist_alg, file = open(os.path.join(out_dir,'filtering_log.txt'), 'w'))
    print('Input bam: %s' % input_bam, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    print('Path to output bam files: %s' % split_dir, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    print('Estimated number of cells: %d' % est_num_cells, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    print('Output directory: %s' % out_dir, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    #print('Barcode directory: %s' % bc_dir, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    print('Barcode legend file: %s' % bc_file, file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))

    if store_discarded:
        if os.path.isfile(os.path.join(out_dir,'discarded_reads.txt')):
            os.remove(os.path.join(out_dir,'discarded_reads.txt'))
            print('Old version of discarded reads.txt deleted.')

    ### Run Filtering
    if multi:
        ## start multithreaded filtering   
        
        files = glob.glob(os.path.join(split_dir, 'x*.bam'))
        ncores = len(files)
        multifilter = Pool(processes=ncores)
        results = multifilter.map(spatialfilter, files)

        # extract recording dictionaries
        record_dicts = [elem[0] for elem in results]

        # Sum up the recording dictionaries
        first_level = ['total_count', 'total_count_kept', 'all_direct']
        record_dict = sum_dicts([{k: d[k] for k in first_level} for d in record_dicts])

        second_level = ['direct', 'corrected']

        for name in coord_names:
            dicts_per_name = [d[name] for d in record_dicts]
            record_dict[name] = sum_dicts([{k: d[k] for k in second_level} for d in dicts_per_name])
    
            record_dict[name]['well_counts'] = sum_dicts([d[name]['well_counts'] for d in record_dicts])


        # extract recording values outside of recording dictionary
        all_bcs = [item for sublist in [elem[1] for elem in results] for item in sublist]
        n_all_bcs = np.array([elem[2] for elem in results]).sum()

    else:
        # without multithreading
        ncores = 1
        results = spatialfilter(input_bam)
        record_dict, all_bcs, n_all_bcs = results

    filter_stop = datetime.now()
    filter_elapsed = filter_stop - t_start


    # Store recording dictionary as .json
    with open(os.path.join(out_dir, 'recording_dictionary.json'), 'w') as fp:
        json.dump(record_dict, fp)

    ## Saving
    if feat_file is not None:
        if not np.any([elem.startswith("feat_") for elem in dge.index]):
            # add feat_ prefix to feature names
            dge.rename(index=lambda s: "feat_" + s, inplace=True)

        # save feature DGE matrix file as .txt
        dge_out_file = os.path.join(out_dir, "DGE_matrix_features.txt.gz")
        print('Write feature DGE matrix to {}...'.format(dge_out_file), flush=True)
        dge.to_csv(dge_out_file, sep = '\t', compression= 'gzip', index=True, header=True)

        # combining RNA and feature reads
        rna = pd.read_csv(rna_dge_file, compression='gzip', sep='\t', index_col=0)
        combined = pd.concat([rna, dge], axis=0)
        combined_out_file = os.path.join(out_dir, "DGE_matrix_rna_with_features.txt.gz")
        print('Write combined DGE matrix to {}...'.format(combined_out_file), flush=True)
        combined.to_csv(combined_out_file, sep = '\t', compression= 'gzip', index=True, header=True)

    # Print to log file
    print('Read %d entries' % record_dict['total_count'], file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    print('Found %d [%.2f%%] complete spot barcodes' % (record_dict['all_direct'], record_dict['all_direct']/float(record_dict['total_count'])*100), file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    for name in coord_names:
            print('Found %d [%.2f%%] expected %s-coordinates, with %s matching %d [%.2f%%] (distance: %d)' % (record_dict[name]['direct'] , 
                record_dict[name]['direct'] / float(record_dict['total_count']) * 100, name, dist_alg, 
                record_dict[name]['corrected'], record_dict[name]['corrected']/float(record_dict['total_count'])*100, dist_thresholds[name]),
            file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
    
    print('Retained %d [%.2f%%] reads after %s matching and filtering' % (record_dict['total_count_kept'], record_dict['total_count_kept']/record_dict['total_count']*100, 
        dist_alg), file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))

    if feat_file is not None:
        print('', file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Statistics about DGE matrix generation for feature reads:', file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('RNA cell list retrieved from: ' + str(rna_dge_file), file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Found %d [%.2f%%] expected feature barcodes' % (record_dict['features']['direct'], record_dict['features']['direct']/record_dict['total_count']*100), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Found %d [%.2f%%] expected feature barcodes with distance %d' % (record_dict['features']['corrected'], record_dict['features']['corrected']/record_dict['total_count']*100, feature_dist), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Found %d [%.2f%%] complete cellular barcodes in RNA cell list' % (record_dict['features']['spot_found'], record_dict['features']['spot_found']/record_dict['total_count']*100), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Found %d [%.2f%%] cases where UMI was found directly in UMI list and was not added to DGE matrix.' % (record_dict['features']['umi_found'], record_dict['features']['umi_found']/record_dict['total_count']*100), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Found %d [%.2f%%] unique UMIs with correct cell and feature barcode that were added to the DGE matrix' % (record_dict['features']['added'], record_dict['features']['added']/record_dict['total_count']*100), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        
        # write log for antibody features
        for v, b in zip(feature_legend.Feature.values, feature_legend.Barcode.values):
            print('Antibody feature %s (%s) was found %d times' % (v, b, record_dict['features'][v]), 
                    file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        
        if interact_mode:
            print('-------------------------------', file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Interaction reads', file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Found %d [%.2f%%] expected feature barcodes' % (record_dict['interact']['direct'], record_dict['interact']['direct']/record_dict['total_count']*100), 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Found %d [%.2f%%] expected feature barcodes with distance %d' % (record_dict['interact']['corrected'], record_dict['interact']['corrected']/record_dict['total_count']*100, feature_dist), 
                    file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Found %d [%.2f%%] complete cellular barcodes in RNA cell list' % (record_dict['interact']['spot_found'], record_dict['interact']['spot_found']/record_dict['total_count']*100), 
                    file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Found %d [%.2f%%] cases where UMI was found directly in UMI list and was not added to DGE matrix.' % (record_dict['interact']['umi_found'], record_dict['interact']['umi_found']/record_dict['total_count']*100), 
                    file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            print('Found %d [%.2f%%] unique UMIs with correct cell and feature barcode that were added to the DGE matrix' % (record_dict['interact']['added'], record_dict['interact']['added']/record_dict['total_count']*100), 
                    file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
            # write log for interaction features
            for c in record_dict['interact'].keys():
                if c not in ["direct", "corrected", "spot_found", "umi_found", "added"]:
                    print('Interaction feature %s was found %d times' % (c, record_dict['interact'][c]), 
                            file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
                
        
        print('Feature spot-count matrix was saved into %s.' % dge_out_file, 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))
        print('Combined spot-count matrix was saved into %s.' % combined_out_file, 
                file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))

    print('Elapsed time for filtering: %s' % str(filter_elapsed), file = open(os.path.join(out_dir,'filtering_log.txt'),'a'))

    # Create plate overview
    print("Generate summary...", flush=True)
    bc_matrices = {name: make_plate_overview(record_dict[name]['well_counts']) for name in coord_names}

    # This part is super slow if you have a large number of cells, maybe omit
    all_bc_counts = {i:all_bcs.count(i) for i in list(set(all_bcs))}

    # Calculate cumulative fraction of reads
    all_bc_cumsum = np.cumsum(sorted(list(all_bc_counts.values()), reverse=True))/n_all_bcs
    
    # plot overview of plate
    plot_plate_overview(bc_matrices, all_bc_cumsum, coord_names, est_num_cells, out_dir)

    ## Stop timing
    t_stop = datetime.now()
    t_elapsed = t_stop-t_start

    ## Save the QC output
    f = h5py.File(os.path.join(out_dir,'abxdbit_filtering_QC_data.hdf5'), 'w')

    for name in coord_names:
        f.create_dataset('BC{}_plate_overview'.format(name), data = bc_matrices[name])

    f.create_dataset('reads_per_BC', data=list(all_bc_counts.values()))
    f.create_dataset('labels_reads_per_BC', data=np.string_(list(all_bc_counts.keys())))

    f.close()

    if feat_file is not None:
        # Save the UMI dictionary as .json
        print('Saving UMI dictionary...', flush=True)
        umi_dict_file = open(os.path.join(tmp_dir, 'umi_dictionary.json'), 'w')
        json.dump(umi_dict, umi_dict_file)
        umi_dict_file.close()
        
    # save record dict
    print('Saving record dictionary...', flush=True)
    record_dict_file = open(os.path.join(tmp_dir, 'record_dictionary.json'), 'w')
    json.dump(record_dict, record_dict_file)
    record_dict_file.close()

    ## print info to stdout
    print("Summary generated. Elapsed time: " + str(t_elapsed), flush=True)