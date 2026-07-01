"""
Generates cozy-themed GitHub stats for the profile README.
Adapted from Andrew6rant/Andrew6rant's today.py — same underlying technique
(GitHub GraphQL API + a local commit-count cache + editing an SVG template
by element id) trimmed down to just: commits, repos, stars, and total
lines of code.

Why this exists instead of a live badge: GitHub's API has no "total lines
of code" field. The only way to get a real number is to walk every commit
in every repo and sum additions/deletions for commits you authored — which
is too slow to do on every profile view, so it's done here on a schedule
instead and the result is baked into an SVG that's committed to the repo.
"""
import os
import time
import hashlib
import requests
from lxml import etree

HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
CACHE_COMMENT_SIZE = 1  # number of comment lines at the top of the cache file
CACHE_FILE = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'


def simple_request(name, query, variables):
    r = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if r.status_code == 200:
        return r
    raise Exception(name, 'failed with', r.status_code, r.text)


def user_getter(username):
    query = '''
    query($login: String!){
        user(login: $login) { id }
    }'''
    r = simple_request('user_getter', query, {'login': username})
    return {'id': r.json()['data']['user']['id']}


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges { node { ... on Repository { nameWithOwner stargazers { totalCount } } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    r = simple_request('graph_repos_stars', query, variables)
    data = r.json()['data']['user']['repositories']
    if count_type == 'repos':
        return data['totalCount']
    elif count_type == 'stars':
        return sum(edge['node']['stargazers']['totalCount'] for edge in data['edges'])


def recursive_loc(owner, repo_name, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Walks a repo's commit history 100 commits at a time, summing LOC for commits authored by OWNER_ID."""
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges { node { ... on Commit { author { user { id } } } deletions additions } }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    r = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if r.status_code != 200:
        if r.status_code == 403:
            raise Exception('Hit the GitHub anti-abuse rate limit — try again later')
        raise Exception('recursive_loc failed with', r.status_code, r.text)
    branch = r.json()['data']['repository']['defaultBranchRef']
    if branch is None:  # empty repo
        return addition_total, deletion_total, my_commits
    history = branch['target']['history']
    for edge in history['edges']:
        if edge['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += edge['node']['additions']
            deletion_total += edge['node']['deletions']
    if not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    return recursive_loc(owner, repo_name, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, cursor=None, edges=None):
    """Fetches every repo the user has access to, along with each repo's current total commit count
    (used to decide whether that repo needs to be re-walked, or can be served from cache)."""
    if edges is None:
        edges = []
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef { target { ... on Commit { history { totalCount } } } }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    r = simple_request('loc_query', query, variables)
    repos = r.json()['data']['user']['repositories']
    edges += repos['edges']
    if repos['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, repos['pageInfo']['endCursor'], edges)
    return cache_builder(edges)


def cache_builder(edges):
    """Compares each repo's current commit count against the cached value; only re-walks repos that changed."""
    try:
        with open(CACHE_FILE, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = ['# cache: repo_hash total_commits my_commits loc_add loc_del\n']
        with open(CACHE_FILE, 'w') as f:
            f.writelines(data)

    if len(data) - CACHE_COMMENT_SIZE != len(edges):
        # repo count changed — rebuild the cache skeleton
        comment = data[:CACHE_COMMENT_SIZE]
        with open(CACHE_FILE, 'w') as f:
            f.writelines(comment)
            for edge in edges:
                f.write(hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')
        with open(CACHE_FILE, 'r') as f:
            data = f.readlines()

    comment = data[:CACHE_COMMENT_SIZE]
    data = data[CACHE_COMMENT_SIZE:]
    for i, edge in enumerate(edges):
        repo_hash, total_commits, *_ = data[i].split()
        branch = edge['node']['defaultBranchRef']
        current_total = branch['target']['history']['totalCount'] if branch else 0
        if int(total_commits) != current_total:
            owner, repo_name = edge['node']['nameWithOwner'].split('/')
            add, delete, mine = recursive_loc(owner, repo_name)
            data[i] = f"{repo_hash} {current_total} {mine} {add} {delete}\n"

    with open(CACHE_FILE, 'w') as f:
        f.writelines(comment)
        f.writelines(data)

    loc_add = sum(int(line.split()[3]) for line in data)
    loc_del = sum(int(line.split()[4]) for line in data)
    commits = sum(int(line.split()[2]) for line in data)
    return loc_add, loc_del, loc_add - loc_del, commits


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = '{:,}'.format(new_text)
    new_text = str(new_text)
    el = root.find(f".//*[@id='{element_id}']")
    if el is not None:
        el.text = new_text
    just_len = max(0, length - len(new_text))
    dot_string = '' if just_len == 0 else (' ' if just_len == 1 else ' ' + ('.' * just_len) + ' ')
    dots_el = root.find(f".//*[@id='{element_id}_dots']")
    if dots_el is not None:
        dots_el.text = dot_string


def svg_overwrite(filename, commit_data, star_data, repo_data, loc_net, loc_add, loc_del):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'loc_data', loc_net, 9)
    justify_format(root, 'loc_add', loc_add)
    justify_format(root, 'loc_del', loc_del, 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


if __name__ == '__main__':
    print('Fetching account info...')
    OWNER_ID = user_getter(USER_NAME)

    print('Walking commit history for total LOC + commits (this is the slow part)...')
    start = time.perf_counter()
    loc_add, loc_del, loc_net, commit_data = loc_query(['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    print(f'  done in {time.perf_counter() - start:.1f}s')

    star_data = graph_repos_stars('stars', ['OWNER'])
    repo_data = graph_repos_stars('repos', ['OWNER'])

    svg_overwrite('stats.svg', commit_data, star_data, repo_data, loc_net, loc_add, loc_del)
    print(f'Repos: {repo_data} | Stars: {star_data} | Commits: {commit_data} | LOC: +{loc_add} -{loc_del} = {loc_net}')
