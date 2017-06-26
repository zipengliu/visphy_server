from flask import Flask, jsonify, request
from flask_compress import Compress
from flask_cors import CORS
from pymongo import MongoClient
from dendropy import Tree, TaxonNamespace
import sys, os, subprocess
from datetime import datetime
import uuid

COMPRESS_LEVEL = 6
TEMP_CONSENSUS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'temp_consensus')
CONSENSUS_PARALLIZATION = '4'


class Connection():
    def __init__(self, env):
        self.url = 'mongodb://visphybe.cs.ubc.ca:27017/visphy_dev' if env == 'prod' else 'mongodb://localhost:27017/visphy_dev'
        self.mongo_client = MongoClient(self.url)
        self.mongo_db = self.mongo_client.visphy_dev
        self.input_group = self.mongo_db.inputGroup
        self.entity = self.mongo_db.entity
        self.tree = self.mongo_db.tree
        self.branch = self.mongo_db.branch

    def test_connection(self):
        n = self.input_group.find({}).count()
        print 'MongoDB connected.'
        print 'There are', n, 'datasets in the database.'


env = 'dev'
if len(sys.argv) > 1:
    env = sys.argv[1]
connection = Connection(env)
connection.test_connection()
app = Flask('Visphy Server ' + env)
Compress(app)
CORS(app)


@app.route('/')
def hello_world():
    return 'Hello World!'


@app.route('/datasets')
def get_datasets():
    cursor = connection.input_group.find({})
    data = [{'inputGroupId': d['inputGroupId'], 'title': d['title'], 'description': d.get('description', ''),
             'numTrees': len(d['trees'])}
            for d in cursor]
    return jsonify(data)


@app.route('/dataset/<int:input_group_id>')
def get_dataset(input_group_id):
    print 'Getting dataset', input_group_id
    data = connection.input_group.find_one({'inputGroupId': input_group_id}, projection={'trees': False})
    entity_cursor = connection.entity.find({'inputGroupId': input_group_id}, projection={'eid': True, 'name': True})
    tree_cursor = connection.tree.find({'inputGroupId': input_group_id},
                                       projection={'name': True, 'tid': True, 'entities': True, 'rfDistance': True, 'rootBranch': True})
    branch_cursor = connection.branch.find({'inputGroupId': input_group_id},
                                           projection={'inputGroupId': False, 'cb': False, 'cb2': False, 'parent': False, 'isLeaf': False})
    ref_branch_cursor = connection.branch.find({'inputGroupId': input_group_id, 'tid': data['defaultReferenceTree']},
                                               projection={'inputGroupId': False, 'tid': False, 'parent': False, 'isLeaf': False})

    del data['_id']
    trees = {}
    for d in tree_cursor:
        trees[d['tid']] = d
        trees[d['tid']]['branches'] = {}
        del d['_id']
    for d in branch_cursor:
        trees[d['tid']]['branches'][d['bid']] = d
        del d['_id']
        del d['bid']
        del d['tid']
    entities = {}
    for d in entity_cursor:
        entities[d['eid']] = d
        del d['_id']

    ref_tree = trees.pop(data['defaultReferenceTree'])
    for d in ref_branch_cursor:
        ref_tree['branches'][d['bid']] = d
        del d['_id']
        del d['bid']

    data.update({
        'trees': trees,
        'entities': entities,
        'referenceTree': ref_tree
    })

    return jsonify(data)


def read_tree_from_consensus_report(filepath):
    newick_string = None
    with open(filepath, 'r') as f:
        mark = False
        for line in f:
            if mark:
                newick_string = line.strip()
                break
            if line.startswith('BEGIN TREES'):
                mark = True
    newick_string = newick_string[newick_string.find('('):]
    return newick_string


def parse_tree(tree, entities):
    tree.ladderize()

    branches = {}
    no = 0
    for node in tree.levelorder_node_iter():
        assert node.is_leaf() or node.num_child_nodes() == 2
        node.bid = 'b' + str(no)
        no += 1

    present_entities = {}
    for node in tree.levelorder_node_iter():
        if node.is_leaf():
            label = node.taxon.label.replace(' ', '_')
            branches[node.bid] = {
                'entity': entities[label],
                # 'support': node.support,
                'length': node.edge_length
            }
            present_entities[entities[label]] = True
        else:
            branches[node.bid] = {
                'left': node.child_nodes()[0].bid,
                'right': node.child_nodes()[1].bid,
                'support': float(node.label),
                'length': node.edge_length
            }

    data = {
        'name': 'Consensus tree',
        'branches': branches,
        'entities': present_entities.keys(),
        'rfDistance': {},       # Cost is too high?
        'rootBranch': 'b0',
        'tid': 'consensus-' + str(uuid.uuid4())
    }

    return data


@app.route('/consensus', methods=['POST'])
def get_consensus():
    input_group_id = int(request.form['inputGroupId'])
    tids = str(request.form['trees']).split(',')
    print 'Creating consensus tree for ', tids, 'in dataset', input_group_id

    # Retrieve the tree data (newick string) from the DB
    tree_cursor = connection.tree.find({'inputGroupId': input_group_id}, projection={'tid': True, 'newickString': True})
    trees = {tid: True for tid in tids}
    remain = len(tids)
    tree_str = []
    for d in tree_cursor:
        if d['tid'] in trees:
            tree_str.append(d['newickString'])
            remain -= 1
            if remain == 0:
                break
    assert remain == 0, 'Failed to find newick string for some trees'

    # Write the strings to a file
    tree_filename = datetime.now().strftime('%m%d%H%M%S%f') + '.tre'
    tree_path = os.path.join(TEMP_CONSENSUS_PATH, tree_filename)
    with open(tree_path, 'w') as f:
        f.write('\n'.join(tree_str))

    # Invoke sumtrees.py
    consensus_path = os.path.join(TEMP_CONSENSUS_PATH, tree_filename + '.consensus')
    try:
        subprocess.check_call(['sumtrees.py', '--output-tree-filepath=' + consensus_path, '--suppress-annotations',
                               '--multiprocessing=' + CONSENSUS_PARALLIZATION, tree_path],
                              stdout=open(os.devnull, 'w'), stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        # TODO
        pass

    # Read the entity mapping from DB
    entity_cursor = connection.entity.find({'inputGroupId': input_group_id}, projection={'eid': True, 'name': True})
    entities = {str(e['name']): str(e['eid']) for e in entity_cursor}

    # Read the consensus tree and parse
    newick_string = read_tree_from_consensus_report(consensus_path)
    tree = Tree.get(data=newick_string, schema='newick')
    return jsonify(parse_tree(tree, entities))

if __name__ == '__main__':
    app.run(port=33333)
