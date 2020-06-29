#!/usr/bin/env python3
# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""Rank documents with TF-IDF scores"""

import logging
import numpy as np
import scipy.sparse as sp

from multiprocessing.pool import ThreadPool
from functools import partial

import tfidf_util as utils
from simple_tokenizer import SimpleTokenizer

logger = logging.getLogger(__name__)


class TfidfDocRanker(object):
    """Loads a pre-weighted inverted index of token/document terms.
    Scores new queries by taking sparse dot products.
    """

    def __init__(self, tfidf_path=None, strict=True):
        """
        Args:
            tfidf_path: path to saved model file
            strict: fail on empty queries or continue (and return empty result)
        """
        # Load from disk
        tfidf_path = tfidf_path
        logger.info('Loading %s' % tfidf_path)
        matrix, metadata = utils.load_sparse_csr(tfidf_path)
        self.doc_mat = matrix
        self.ngrams = metadata['ngram']
        self.hash_size = metadata['hash_size']
        self.tokenizer = SimpleTokenizer()
        self.doc_freqs = metadata['doc_freqs'].squeeze()
        self.doc_dict = metadata['doc_dict']
        self.num_docs = len(self.doc_dict[0])
        self.strict = strict

    def get_doc_index(self, doc_id):
        """Convert doc_id --> doc_index"""
        return self.doc_dict[0][doc_id]

    def get_doc_id(self, doc_index):
        """Convert doc_index --> doc_id"""
        return self.doc_dict[1][doc_index]

    def doc_meta(self, pmid_or_title):
        """Convert pmid --> doc_meta"""
        if len(self.doc_dict) >= 3:
            return self.doc_dict[2].get(pmid_or_title, {})
        else:
            return {}

    def batch_doc_meta(self, pmids, num_workers=None):
        with ThreadPool(num_workers) as threads:
            results = threads.map(self.doc_meta, pmids)
        return results

    def closest_docs(self, query, meta, meta_scale, k=1):
        """Closest docs by dot product between query and documents
        in tfidf weighted word vector space.
        """
        spvec = self.text2spvec(query)
        res = spvec * self.doc_mat

        # Add meta scores
        if len(self.doc_dict) == 4:
            for m in meta:
                if query in self.doc_dict[3][m][0]:
                    res[0,self.doc_dict[3][m][0][query]] += np.array(self.doc_dict[3][m][1][query]) * meta_scale

        if len(res.data) <= k:
            o_sort = np.argsort(-res.data)
        else:
            o = np.argpartition(-res.data, k)[0:k]
            o_sort = o[np.argsort(-res.data[o])]

        doc_scores = res.data[o_sort]
        # doc_ids = [self.get_doc_id(i) for i in res.indices[o_sort]]
        doc_ids = [int(i) for i in res.indices[o_sort]] # TODO if none is returned?
        return doc_ids, doc_scores

    def batch_closest_docs(self, queries, meta=['covidex'], meta_scale=100, k=1, num_workers=None):
        """Process a batch of closest_docs requests multithreaded.
        Note: we can use plain threads here as scipy is outside of the GIL.
        """
        with ThreadPool(num_workers) as threads:
            closest_docs = partial(self.closest_docs, meta=meta, meta_scale=meta_scale, k=k)
            results = threads.map(closest_docs, queries)
        return results

    def doc_scores(self, query_doc, meta, meta_scale):
        """Get doc scores by dot product between query and documents
        in tfidf weighted word vector space.
        """
        query, doc_idx = query_doc
        spvec = self.text2spvec(query)
        res = spvec * self.doc_mat

        # Add meta scores
        if len(self.doc_dict) == 4:
            for m in meta:
                if query in self.doc_dict[3][m][0]:
                    res[0,self.doc_dict[3][m][0][query]] += np.array(self.doc_dict[3][m][1][query]) * meta_scale
                # scores += self.doc_dict[3][m][doc_idx]

        scores = res[0,doc_idx].toarray()[0]
        return scores.tolist()

    def batch_doc_scores(self, queries, doc_idxs, meta=['covidex'], meta_scale=100, num_workers=None):
        """Process a batch of doc_scores requests multithreaded.
        Note: we can use plain threads here as scipy is outside of the GIL.
        """
        with ThreadPool(num_workers) as threads:
            doc_scores = partial(self.doc_scores, meta=meta, meta_scale=meta_scale)
            results = threads.map(doc_scores, zip(queries, doc_idxs))
        return results

    def parse(self, query):
        """Parse the query into tokens (either ngrams or tokens)."""
        tokens = self.tokenizer.tokenize(query)
        return tokens.ngrams(n=self.ngrams, uncased=True,
                             filter_fn=utils.filter_ngram)

    def text2spvec(self, query, val_idx=False):
        """Create a sparse tfidf-weighted word vector from query.

        tfidf = log(tf + 1) * log((N - Nt + 0.5) / (Nt + 0.5))
        """
        # Get hashed ngrams
        words = self.parse(utils.normalize(query))
        wids = [utils.hash(w, self.hash_size) for w in words]

        if len(wids) == 0:
            if self.strict:
                raise RuntimeError('No valid word in: %s' % query)
            else:
                logger.warning('No valid word in: %s' % query)
                if val_idx:
                    return np.array([]), np.array([])
                else:
                    return sp.csr_matrix((1, self.hash_size))

        # Count TF
        wids_unique, wids_counts = np.unique(wids, return_counts=True)
        tfs = np.log1p(wids_counts)

        # Count IDF
        Ns = self.doc_freqs[wids_unique]
        idfs = np.log((self.num_docs - Ns + 0.5) / (Ns + 0.5))
        idfs[idfs < 0] = 0

        # TF-IDF
        data = np.multiply(tfs, idfs)

        if val_idx:
            return data, wids_unique

        # One row, sparse csr matrix
        indptr = np.array([0, len(wids_unique)])
        spvec = sp.csr_matrix(
            (data, wids_unique, indptr), shape=(1, self.hash_size)
        )

        return spvec
