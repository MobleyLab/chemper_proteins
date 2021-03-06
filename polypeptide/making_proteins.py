"""
making_proteins.py

It turns out that getting a protein that is parameterized by the
desired force field where you also have a complete molecular
description (bond order, etc) was harder than it sounded.

This script is a bit convoluted and given more time at the end
of Caitlin's PhD she would have cleaned it up, but instead
there are notes to try to explain what it does.

The ultimate goal is this:
1. start with a fasta file of the amino acid chain you want
   these were chosen as the file format because it is easy to get
   an arbitrary order of amino acids so we could potentially make
   any combination of small polypeptides for testing this process.
   It should be simple to switch this to a PDB instead

2. Use oeommtools to convert an OEMol to an OpenMM system which
   can be parameterized with any OpenMM XML force field

3. For each fragment type, make clusters of atoms based on the
   parameter that is assigned.
   For example, bond are grouped by there exact force constant
   and equilibrium force constant.
   Note - this is done by converting parameters to strings which
   can be used in keys of a dictionary to store atoms in that cluster.
   No, that isn't the best or most elegant way to do this, but it worked
   in the short time frame for wrapping up the project.

4. Convert dictionaries of {parameter: atoms} to clusters for ChemPer
   We order these clusters in a few different was as described in the
   manuscript.

5. Use ChemPer to try to make SMIRKS for each fragment type and
   and ordering technique
"""

import os
import copy
import random
import parmed
from parmed.modeller import ResidueTemplate
from simtk.openmm import app
from simtk import unit
from oeommtools import utils as oeo_utils
from chemper.smirksify import SMIRKSifier
from chemper.graphs.cluster_graph import ClusterGraph
from openeye import oechem


class ParameterDict:
    """
    This class makes it easier to store a custom organized dictionary
    for any parameter type.
    In 'd' the key is always going to be a string of the parameter
    then we store the following information:

    atom_indices: this is dictionary in the form {mol_id: [ (atoms), ]}
    parameters: since we're using strings of the parameter as a key
                this is a set of all parameters that were actually used
                the goal was to use this to check that there was REALLY
                only 1 parameter added to each item, however we never
                actually used it for anything.
    units: units for the input parameters.
    """
    def __init__(self):
        self.d = dict()

    def items(self):
        return self.d.items()

    def add_key(self, key):
        if key not in self.d:
            self.d[key] = {'atom_indices': dict(), 'parameters': set(), 'units': None}

    def add_atoms(self, key, mol_id, atom_tuple):
        self.add_key(key)
        if mol_id not in self.d[key]['atom_indices']:
            self.d[key]['atom_indices'][mol_id] = list()
        self.d[key]['atom_indices'][mol_id].append(tuple(atom_tuple))

    def add_param(self, key, params):
        self.add_key(key)
        new_tuple = [x._value for x in params]
        self.d[key]['parameters'].add(tuple(new_tuple))
        self.d[key]['units'] = tuple([x.unit for x in params])


class ParameterSystem:
    """
    Like ParameterDict, this class was created
    to try to help with bookkeeping when converting
    a molecule from an oemol to a parameterized openmm system.
    It has functions to add molecules into the system and add
    there atoms to ParameterDicts for each fragment type.
    """

    def __init__(self, openmm_xml='amber99sbildn.xml'):
        self.openmm_xml = openmm_xml
        self.lj_dict = ParameterDict()
        self.charge_dict = ParameterDict()
        self.bond_dict = ParameterDict()
        self.angle_dict = ParameterDict()
        self.proper_dict = ParameterDict()
        self.improper_dict = ParameterDict()
        self.mol_dict = dict()

    def add_system_from_fasta(self, fasta):
        """
        When working with fasta files, this function does the bulk of
        the work for converting that fasta file to an OEMol and
        then making an OpenMM system and identifying parameters by atom.

        For all tests in this manuscript we started with the file
        mol_files/everything.fasta
        """
        base = os.path.abspath(fasta).split('.')[0]
        mol_id = base.split('/')[-1]

        oemol = oechem.OEMol()
        ifs = oechem.oemolistream(fasta)

        # After a lot of working on a single example
        # this seems to be the right combination
        # of calls to get an oemol from a fasta into the form
        # where all bonds are fully perceived with order, etc.
        # and that allows oeommtools to convert
        # the molecule into an OpenMM system.
        # I don't have justification for most of the steps.
        oechem.OEReadFASTAFile(ifs, oemol)
        oechem.OEAddExplicitHydrogens(oemol)
        oechem.OEPerceiveResidues(oemol)
        oechem.OEPDBOrderAtoms(oemol)

        ofs = oechem.oemolostream()
        ofs.SetFormat(oechem.OEFormat_PDB)
        ofs.openstring()
        oechem.OEWriteMolecule(ofs, oemol)

        ifs = oechem.oemolistream()
        ifs.openstring(ofs.GetString())
        m = oechem.OEMol()
        oechem.OEReadPDBFile(ifs, m)
        m.SetTitle(mol_id)

        # convert oemol to OpenMM topology
        top = oeo_utils.oemol_to_openmmTop(m)[0]
        ff = app.ForceField(self.openmm_xml)
        protein_sys = ff.createSystem(top)

        oechem.OEAssignFormalCharges(m)
        oechem.OEClearAromaticFlags(m)
        # IMPORTANT!!!!
        # use MDL aromaticity model to be consistent with SMIRNOFF
        oechem.OEAssignAromaticFlags(m, oechem.OEAroModel_MDL)
        oechem.OEAssignHybridization(m)

        # save residue names in parm system
        # We had issues with charges because they are different
        # depending where they are in the residue chain, terminal
        # residues have different charges than those in the
        # main chain. More work is still require to figure out how
        # properly handle charges
        parm = parmed.openmm.load_topology(top, protein_sys)
        for res in parm.residues:
            rt = ResidueTemplate.from_residue(res)
            if rt.tail is None and rt.head is not None:
                res.name = 'C_'+res.name
            elif rt.head is None and rt.tail is not None:
                res.name = 'N_'+res.name

        self.mol_dict[mol_id] = {
            'parmed': parm,
            'oemol': oechem.OEMol(m)
        }
        self._add_parameters_from_system(parm, mol_id)

        return parm, m

    def _add_parameters_from_system(self, sys, mol_id):
        self.add_nonbonds(sys, mol_id)
        self.add_bonds(sys, mol_id)
        self.add_angles(sys, mol_id)
        self.add_torsions(sys, mol_id)

    def add_nonbonds(self, sys, mol_id):
        """
        Updates LJ and charge dictionaries for this system.
        Parameters
        ----------
        sys: parmed system
        mol_id: key for this system to store data in the dictionaries
        """
        # TODO raise error if mol_id not in mol_dict
        for a in sys.atoms:
            # find terminal label:
            term_label = 'X'
            if 'N_' in a.residue.name:
                term_label = 'N'
            elif 'C_' in a.residue.name:
                term_label = 'C'

            # Update charge dictionary:
            charge_str = "%.5f\t%s" % (a.charge, term_label)
            charge_param = [a.ucharge]
            self.charge_dict.add_param(charge_str, charge_param)
            self.charge_dict.add_atoms(charge_str, mol_id, [a.idx])

            # Update LJ dictionary
            lj_str = "%.3f\t%.3f" % (a.epsilon, a.rmin)
            lj_params = [a.uepsilon, a.urmin]
            self.lj_dict.add_param(lj_str, lj_params)
            self.lj_dict.add_atoms(lj_str, mol_id, [a.idx])

    def add_bonds(self, sys, mol_id):
        """
        Updates the bond parameter dictionary for the input system.
        Parameters
        ----------
        sys: parmed system
        mol_id: key for this system to store data in the dictionaries
        """
        # TODO raise error if mol_id not in mol_dict
        for b in sys.bonds:
            bond_str = "%.3f\t%.3f" % (b.type.k, b.type.req)
            bond_params = [b.type.uk, b.type.ureq]
            self.bond_dict.add_param(bond_str, bond_params)
            self.bond_dict.add_atoms(bond_str, mol_id, [b.atom1.idx, b.atom2.idx])

    def add_angles(self, sys, mol_id):
        """
        Updates the angle parameter dictionary for the input system.
        Parameters
        ----------
        sys: parmed system
        mol_id: key for this system to store data in the dictionaries
        """
        # TODO raise error if mol_id not in mol_dict
        for an in sys.angles:
            angle_str = "%.3f\t%.3f" % (an.type.k, an.type.theteq)
            angle_params = [an.type.uk, an.type.utheteq]
            self.angle_dict.add_param(angle_str, angle_params)
            self.angle_dict.add_atoms(angle_str, mol_id, [an.atom1.idx, an.atom2.idx, an.atom3.idx])

    def convert_for_smirksifying(self, param_type=None):
        """

        Parameters
        ----------
        param_type: string specifying the parameter you want clusters
                    must chose from ['lj', 'charge', 'proper_torsion',
                    'improper_torsion', 'angle', 'bond']
                    If parameter type is None, a dictionary with all
                    clusters is returned instead

        Returns
        -------
        mols: list of OEMols
        clusters: if param_types is None: dictionary if a list of
                  clusters for each parameter type. Otherwise,
                  the list of clusters for the specified parameter type
        """
        idx_list = list()
        mol_list = list()
        cluster_types = dict()

        dictionaries = {
            'lj': self.lj_dict,
            'charge': self.charge_dict,
            'proper_torsion': self.proper_dict,
            'improper_torsion': self.improper_dict,
            'angle': self.angle_dict,
            'bond': self.bond_dict,
        }

        if param_type is not None:
            if param_type.lower() not in dictionaries.keys():
                return cluster_types
            dictionaries = {param_type.lower(): dictionaries[param_type.lower()]}

        for idx, me in self.mol_dict.items():
            idx_list.append(idx)
            mol_list.append(me['oemol'])

        for label, par_dict in dictionaries.items():
            cluster_types[label] = list()
            for cluster_label, entry in par_dict.items():
                atom_list = list()
                for idx in idx_list:
                    if idx in entry['atom_indices']:
                        atom_list.append(entry['atom_indices'][idx])
                    else:
                        atom_list.append(list())
                cluster_types[label].append((cluster_label, atom_list))

        if param_type is None:
            return mol_list, cluster_types

        return mol_list, cluster_types[param_type.lower()]

    def add_torsions(self, sys, mol_id):
        """
        Updates the proper_torsion and improper_torsion
        parameter dictionaries for the input system.
        Parameters
        ----------
        sys: parmed system
        mol_id: key for this system to store data in the dictionaries
        """
        # TODO raise error if mol_id not in mol_dict
        temp_dict = dict()
        for d in sys.dihedrals:
            # Check for Improper
            if d.improper:
                imp_str = "%.3f\t%.3f\t%.3f\t%i" % (d.type.phi_k, d.type.phase, d.type.per, d.atom3.atomic_number)
                imp_params = [d.type.uphi_k, d.type.uphase, unit.Quantity(d.type.per)]
                self.improper_dict.add_param(imp_str, imp_params)

                # order side atoms:
                sides = sorted([d.atom1.idx, d.atom2.idx, d.atom4.idx])
                atom_indices = [sides[0], d.atom3.idx, sides[1], sides[2]]
                self.improper_dict.add_atoms(imp_str, mol_id, atom_indices)
            else: # proper torsion
                atoms = tuple([d.atom1.idx, d.atom2.idx, d.atom3.idx, d.atom4.idx])
                params = (d.type.uphi_k, d.type.uphase, unit.Quantity(d.type.per))
                if atoms not in temp_dict:
                    temp_dict[atoms] = list()
                temp_dict[atoms].append(params)

        # combine parameters with multiple periodicities for the
        # same four atoms so those are treated as one parameter
        for atoms, param_list in temp_dict.items():
            new_params = [p for t in param_list for p in t]
            prop_str = '\t'.join(['%.3f' % p._value for p in new_params])
            self.proper_dict.add_param(prop_str, new_params)
            self.proper_dict.add_atoms(prop_str, mol_id, atoms)


# ==================================================
# Ordering functions
# below are a series of functions that take a list of cluster
# and reorder them

def reverse_clusters(clusters):
    return list(reversed(clusters))

def shuffle(clusters):
    temp_c = copy.deepcopy(clusters)
    random.shuffle(temp_c)
    return temp_c


def by_smallest_size(clusters):
    # smallest cluster by number of atom_indice groups in it
    return sorted(clusters, key=lambda x: len([a for l in x[1] for a in l]))


def by_smallest_num_molecule(clusters):
    temp_c = sorted(clusters, key=lambda x: len([1 for l in x[1] if len(l) > 0]))
    return temp_c


def by_biggest_size(clusters):
    return reverse_clusters(by_smallest_size(clusters))


def by_biggest_num_molecule(clusters):
    return reverse_clusters(by_smallest_num_molecule(clusters))


def by_smallest_smirks(clusters, mols):
    temp_c = sorted(clusters, key=lambda x: len(ClusterGraph(mols, x[1]).as_smirks()))
    return temp_c


def by_biggest_smirks(clusters, mols):
    return reverse_clusters(by_smallest_smirks(clusters, mols))


def by_terminii(clusters, mols, sort_funct=by_biggest_size):
    """
    This is used for charge clusters to put special case termini
    clusters at the end of the list
    """
    x = [t for t in clusters if 'X' in t[0]]
    n = [t for t in clusters if 'N' in t[0]]
    c = [t for t in clusters if 'C' in t[0]]
    if sort_funct is None:
        return x + n + c
    if 'smirks' in sort_funct.__str__():
        return sort_funct(x, mols) + sort_funct(n, mols) + sort_funct(c, mols)

    return sort_funct(x) + sort_funct(n) + sort_funct(c)


def change_order_smirksified(mols, cluster_types, order_type_names=None, smirks_verbose=False, include_params=None):
    """
    Creates SMIRKSifier objects for all specified order types.

    Parameters
    ----------
    mols: list of oemols
    cluster_types: dictionary in the form {fragment: clusters}
    order_type_names: list of types of cluster ordering
                      techniques to try, if None all will be used
    smirks_verbose: verbosity input for SMIRKSifier
    include_params: which fragment types (bond, angle, etc) to include
                    if None, all fragments in cluster_types will be used

    Returns
    -------
    smirksifier_order_types: This is a dictionary storing ChemPer
                             SMIRKSifier objects for each order
                             and fragment type used, it has the form:
                             { order type:
                                   fragment: SMIRKSifier}
                            Note - these included FAILED SMIRKSifier
                            objects so it is important to check if the
                            SMIRKSifier was successful.
    """
    smirs_order_types = dict()

    order_types_dict = {
        'original': None,
        'reversed': reverse_clusters,
        'shuffle': shuffle,
        'small_size': by_smallest_size,
        'biggest_size': by_biggest_size,
        'fewest_mols': by_smallest_num_molecule,
        'most_mols': by_biggest_num_molecule,
        'small_smirks': by_smallest_smirks,
        'big_smirks': by_biggest_smirks}

    if order_type_names is None:
        order_type_names = ['shuffle']

    order_types = list()
    for n in set(order_type_names):
        o_funct = order_types_dict.get(n, None)
        order_types.append((n, o_funct))
        for i in range(1, order_type_names.count(n)):
            temp_n = '%s_%i' % (n, i)
            order_types.append((temp_n, o_funct))

    if include_params is None:
        include_params = list(cluster_types.keys())

    for o_type, o_funct in order_types:
        print(o_type)
        smirs_order_types[o_type] = dict()

        for label, clusters in cluster_types.items():
            if label not in include_params:
                continue

            print(label)
            if 'charge' in label.lower():
                o_clusters = by_terminii(clusters, mols, o_funct)
            elif o_funct is None:
                o_clusters = clusters
            else:
                if 'smirks' in o_type:
                    o_clusters = o_funct(clusters, mols)
                else:
                    o_clusters = o_funct(clusters)

            smirs_order_types[o_type][label] = SMIRKSifier(mols, o_clusters,
                                                           max_layers=10,
                                                           strict_smirks=False,
                                                           verbose=smirks_verbose)

    return smirs_order_types


def print_order_type_data(smirks_order_types, print_all=True):
    """
    Slightly complicated function that makes printing output easer to read
    """
    for o_type, order_smirks in smirks_order_types.items():
        final_print = ''
        all_passed = True
        for label, output in order_smirks.items():
            if not output.checks:
                final_print += '%-23s FAILED to make smirks\n' % label
                all_passed = False
            else:
                final_print += '%-23s PASSED\n' % label

        if all_passed:
            print('-' * 80)
            print('%-23s ALL PASSED' % o_type)
            print('-' * 80)
            print(final_print)
        elif print_all:
            print('-' * 80)
            print(o_type)
            print('-' * 80)
            print(final_print)


def at_least_one_passed(smirks_order_types):
    """
    This takes the output from change_order_smirksified and
    then checks all of the SMIRKSifiers to see if at least ONE
    passed!
    """
    passed = True
    for o_type, order_smirks in smirks_order_types.items():
        passed = True
        for label, output in order_smirks.items():
            if not output.checks:
                passed = False
        if passed:
            return True
    return passed


def everything_from_fastas(list_fastas,
                           protein_xlm='amber99sbildn.xml',
                           order_type_names=None,
                           verbose=True,
                           include_params=None):
    """
    Parameters
    ----------
    list_fastas: list of str
                 list of full paths to .fasta files with peptide sequences

    protein_xml: str
                 file name or complete path to a openMM .xml file for assign protein parameters
    order_type_names: list of str

    Returns
    -------
    data_storage: ParameterSystem object
                  ParameterSystem storing the parameterized molecules and clusters
                  by parameter type from the provided fasta files
    smirs_order_types: dictionary with the form...
            {
            order type (ie 'shuffle' or 'smallest_size): {
                param_type (ie 'proper_torsion'): SMIRKSifier object (even if it failed)
                }
            }
    """
    if order_type_names is None:
        order_type_names = ['shuffle']

    store_data = ParameterSystem(openmm_xml=protein_xml)
    for fasta in list_fastas:
        parm, oemol = store_data.add_system_from_fasta(fasta)
    mols, cluster_types = store_data.convert_for_smirksifying()

    if verbose:
        table_form = "%-20s %-10s %-10s %s"
        print('=' * 80)
        print(table_form % ('parameter', 'mols', 'clusters', 'mols in clusters'))
        print('-' * 80)
        for label, clusters in cluster_types.items():
            print(table_form % (label, len(mols), len(clusters), len(clusters[0][1])))
        print('=' * 80)

    smirs_order_types = change_order_smirksified(mols, cluster_types,
                                                 order_type_names=order_type_names,
                                                 include_params=include_params)
    if verbose: print_order_type_data(smirs_order_types)

    return store_data, smirs_order_types, mols, cluster_types


def clusters_to_files(mols, clusters, smirs_order_types, json_file_name, mol_dir='./mol_files/'):
    """
    This converts the output from change_order_smirksified and
    saves the created SMIRKS patterns and molecules to output files.

    Specifically the molecules are stored into oeb files which
    are saved into the speficied directory. The clusters and
    created SMIRKS patterns are saved into a json file so they can be
    extracted and reused

    Parameters
    ----------
    mols: list of OEMols
    clusters: cluster dictionary in the form {fragment: cluster list}
    smirs_order_types: SMIRKSifier dictionary from change_order_smirksified
    json_file_name: str, name of json file to save data
    mol_dir: directory to save output files
    """
    directory = os.path.abspath(mol_dir)

    mfile_names = list()
    for midx, mol in enumerate(mols):
        file_name = mol.GetTitle() + '.oeb'
        mfile_names.append(file_name)

        for param, par_clusters in clusters.items():
            for label, cluster in par_clusters:
                key = param + '_' + label
                if len(cluster[midx]) == 0:
                    entry = 'None'
                else:
                    entry = tuple(['-'.join([str(i) for i in a]) for a in cluster[midx]])
                mol.SetData(key, entry)

        ofs_name = os.path.join(directory, file_name)
        ofs = oechem.oemolostream(ofs_name)
        oechem.OEWriteMolecule(ofs, mol)
        ofs.close()

    order_data = dict()
    for types, smirksifier_dict in smirs_order_types.items():
        order_data[types] = dict()
        for param_type, smirksifier in smirksifier_dict.items():
            order_data[types][param_type] = {
                'checked': smirksifier.checks,
                'type_list': smirksifier.current_smirks
            }

    to_j = {
        'mol_files': mfile_names,
        'smiles': [mol_to_idx_smi(m) for m in mols],
        'clusters': clusters,
        'smirks_lists': order_data
    }

    with open(json_file_name, 'w') as output:
        json.dump(to_j, output)


def mol_to_idx_smi(m):
    """
    make a molecule with atom map indices with the atoms
    current indices
    """
    for a in m.GetAtoms():
        a.SetMapIdx(a.GetIdx() + 1)
    return oechem.OEMolToSmiles(m)


if __name__ == '__main__':
    import glob
    import itertools
    import json
    from optparse import OptionParser

    parser = OptionParser()

    parser.add_option('-f', '--fastas',
                      action='store', type='string', dest='fastas',
                      default='everything.fasta',
                      help="This is a search for fasta files in the provided directory")

    parser.add_option('-d', '--directory',
                      action='store', type='string', dest='directory',
                      default='./mol_files/',
                      help="""This is a relative or absolute path the directory with the fasta
                      files and where output json should be stored""")

    parser.add_option('-x', '--xmls',
                      action='store', type='string', dest='xmls',
                      default='99sbildn',
                      help="Which force fields to test, current options are 14all or 99sbildn or all")

    parser.add_option('-n', '--sim_name',
                      action='store', type='string', dest='sim_name',
                      default='',
                      help="a custom label for this run")

    (opt,args) = parser.parse_args()

    # Find which protein forcefields we are considering
    xml_dict = {'99sbildn':'amber99sbildn.xml', '14all':'amber14-all.xml'}
    if opt.xmls not in xml_dict.keys() and opt.xmls.lower() != 'all':
        parser.print_help()
        parser.error("xml must be in [99sbildn, 14all, all]")

    if opt.xmls.lower() == 'both':
        xml_keys = xml_dict.keys()
    else:
        xml_keys = [opt.xmls]

    xmls = [(k, xml_dict[k]) for k in xml_keys]
    simulation_name = opt.sim_name

    directory = os.path.abspath(opt.directory)
    fastas = glob.glob(os.path.join(directory, opt.fastas))
    fastas = [f for f in fastas if '.fasta' in f]

    all_params = ['charge', 'angle', 'improper_torsion', 'proper_torsion', 'lj', 'bond']
    names_sets = [('big', ['biggest_size', 'most_mols', 'big_smirks'] ),
            ( 'small', ['small_size', 'fewest_mols', 'small_smirks'] ),
            ('shuffle', ['original', 'shuffle', 'shuffle'])]

    for name_lab, names in names_sets:
        print(name_lab)
        for xml_label, protein_xml in xmls:
            print(xml_label)
            for param in all_params:
                print(param)
                _, smirks_order_types, mols, clusters = everything_from_fastas(fastas, verbose=False,
                                                                               order_type_names=names,
                                                                               protein_xml=protein_xml,
                                                                               include_params=[param])
                if at_least_one_passed(smirks_order_types):
                    print('Something PASSED --  ', param)
                else:
                    print('ALL FAILED --  ', param)
                json_file = '%s/%s_%s_%s_%s_%imols.json' % (directory, simulation_name, name_lab, xml_label, param, len(fastas))
                clusters_to_files(mols, clusters, smirks_order_types, json_file)

