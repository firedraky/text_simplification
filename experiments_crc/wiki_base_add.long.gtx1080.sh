#!/usr/bin/env bash


#SBATCH --cluster=gpu
#SBATCH --gres=gpu:1
#SBATCH --partition=gtx1080
#SBATCH --job-name=wiki_base_add
#SBATCH --output=wiki_base_add.out
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --time=6-00:00:00
#SBATCH --qos=long

# Load modules
module restore

# Run the job
srun python ../model/train.py -ngpus 1 -bsize 32 -fw transformer -out wiki_base_add -layer_drop 0.2 -op adagrad -lr 0.1 --mode wiki -nhl 6 -nel 6 -ndl 6 -lc True --it_train True -eval_freq 10000 --use_dataset2 True

