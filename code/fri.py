from algebra import *
from extension_field import ExtensionFieldElement
from merkle import *
from ip import *
from ntt import *
from binascii import hexlify, unhexlify
import math
from hashlib import blake2b

from univariate import *


class Fri:
    class Domain:
        def __init__(self, offset, omega, length):
            self.offset = offset
            self.omega = omega
            self.length = length

        def __call__(self, index):
            return (self.omega ^ index) * self.offset

        def list(self):
            return [(self.omega ^ i) * self.offset for i in range(self.length)]

        def evaluate( self, polynomial ):
            coefficients = polynomial.scale(self.offset).coefficients
            coefficients += [self.omega.field.zero()] * (self.length - len(coefficients))
            return ntt(self.omega, coefficients)

        def xevaluate(self, polynomial):
            xfield = polynomial.coefficients[0].field
            coefficients = [[self.omega.field.zero()] * xfield.modulus.degree()] * (1 + polynomial.degree())
            for i in range(len(coefficients)):
                coefficients[i] += [self.omega.field.zero()] * (xfield.modulus.degree() - len(coefficients[i]))
            scale = [self.offset^i for i in range(len(coefficients))]
            for i in range(len(coefficients)):
                for j in range(xfield.modulus.degree()):
                    coefficients[i][j] *= scale[i]
            transposed = [[self.omega.field.zero()] * self.length] * xfield.modulus.degree()
            for i in range(xfield.modulus.degree()):
                for j in range(len(coefficients)):
                    transposed[i][j] = coefficients[j][i]
            values = [ntt(self.omega, row) for row in transposed]
            xcdwd = [ExtensionFieldElement(Polynomial(
                [values[j][i] for j in range(xfield.modulus.degree())]), xfield) for i in range(self.length)]
            return xcdwd

        def interpolate( self, values ):
            return fast_coset_interpolate(self.offset, self.omega, values)

        def xinterpolate( self, values ):
            return fast_coset_interpolate(values[0].field.lift(self.offset), values[0].field.lift(self.omega), values)

    def __init__(self, offset, omega, initial_domain_length, expansion_factor, num_colinearity_tests):
        self.domain = Fri.Domain(offset, omega, initial_domain_length)
        self.field = omega.field
        self.expansion_factor = expansion_factor
        self.num_colinearity_tests = num_colinearity_tests

        assert(self.num_rounds() >= 1), "cannot do FRI with less than one round"

    def num_rounds(self):
        codeword_length = self.domain.length
        num_rounds = 0
        while codeword_length > self.expansion_factor and 4*self.num_colinearity_tests < codeword_length:
            codeword_length /= 2
            num_rounds += 1
        return num_rounds

    def sample_index(byte_array, size):
        acc = 0
        for b in byte_array:
            acc = (acc << 8) ^ int(b)
        return acc % size

    def sample_indices(self, seed, size, reduced_size, number):
        assert(
            number <= reduced_size), f"cannot sample more indices than available in last codeword; requested: {number}, available: {reduced_size}"
        assert(number <= 2 *
               reduced_size), "not enough entropy in indices wrt last codeword"

        indices = []
        reduced_indices = []
        counter = 0
        while len(indices) < number:
            index = Fri.sample_index(
                blake2b(seed + bytes(counter)).digest(), size)
            reduced_index = index % reduced_size
            counter += 1
            if reduced_index not in reduced_indices:
                indices += [index]
                reduced_indices += [reduced_index]

        return indices

    def eval_domain(self):
        return [self.domain(i) for i in range(self.domain_length)]

    def commit(self, codeword, proof_stream, round_index=0):
        one = self.field.one()
        two = BaseFieldElement(2, self.field)
        omega = self.omega
        offset = self.offset
        trees = []
        codewords = []

        # for each round
        for r in range(self.num_rounds()):
            N = len(codeword)

            # make sure omega has the right order
            assert(omega ^ (N - 1) == omega.inverse()
                   ), "error in commit: omega does not have the right order!"

            # compute and send Merkle root
            tree = SaltedMerkle(codeword)
            root = tree.root
            proof_stream.push(root)

            # prepare next round, but only if necessary
            if r == self.num_rounds() - 1:
                break

            # get challenge
            alpha = self.field.sample(proof_stream.prover_fiat_shamir())

            # collect codeword and tree
            codewords += [codeword]
            trees += [tree]

            # split and fold
            codeword = [two.inverse() * ((one + alpha / (offset * (omega ^ i))) * codeword[i] + (
                one - alpha / (offset * (omega ^ i))) * codeword[N//2 + i]) for i in range(N//2)]

            omega = omega ^ 2
            offset = offset ^ 2

        # send last codeword
        proof_stream.push(codeword)

        # collect last codeword too
        codewords = codewords + [codeword]

        return codewords, trees

    def query(self, current_tree, next_tree, c_indices, proof_stream):
        # infer a and b indices
        a_indices = [index for index in c_indices]
        b_indices = [index + len(current_tree.num_leafs) //
                     2 for index in c_indices]

        # reveal leafs
        for s in range(self.num_colinearity_tests):
            proof_stream.push(
                (current_tree.leafs[a_indices[s]], current_tree.leafs[b_indices[s]], next_tree.leafs[c_indices[s]]))

        # reveal authentication paths
        for s in range(self.num_colinearity_tests):
            proof_stream.push(current_tree.open(a_indices[s]))
            proof_stream.push(current_tree.open(b_indices[s]))
            proof_stream.push(next_tree.open(c_indices[s]))

        return a_indices + b_indices

    def prove(self, codeword, proof_stream):
        assert(self.domain_length == len(
            codeword)), "initial codeword length does not match length of initial codeword"

        # commit phase
        codewords, trees = self.commit(codeword, proof_stream)

        # get indices
        top_level_indices = self.sample_indices(proof_stream.prover_fiat_shamir(), len(
            codewords[1]), len(codewords[-1]), self.num_colinearity_tests)
        indices = [index for index in top_level_indices]

        # query phase
        for i in range(len(codewords)-1):
            indices = [index % (len(codewords[i])//2)
                       for index in indices]  # fold
            self.query(codewords[i], codewords[i+1], indices, proof_stream)

        return top_level_indices

    def verify(self, proof_stream, polynomial_values):
        omega = self.omega
        offset = self.offset

        # extract all roots and alphas
        roots = []
        alphas = []
        for r in range(self.num_rounds()):
            roots += [proof_stream.pull()]
            alphas += [self.field.sample(proof_stream.verifier_fiat_shamir())]

        # extract last codeword
        last_codeword = proof_stream.pull()

        # check if it matches the given root
        # how?! We don't have the salts!
        # if roots[-1] != SaltedMerkle(last_codeword).root:
        #     print("last codeword is not well formed")
        #     return False
        # therefore, we don't send the root

        # check if it is low degree
        degree = (len(last_codeword) // self.expansion_factor) - 1
        last_omega = omega
        last_offset = offset
        for r in range(self.num_rounds()-1):
            last_omega = last_omega ^ 2
            last_offset = last_offset ^ 2

        # assert that last_omega has the right order
        assert(last_omega.inverse() == last_omega ^ (
            len(last_codeword)-1)), "omega does not have right order"

        # compute interpolant
        last_domain = [last_offset * (last_omega ^ i)
                       for i in range(len(last_codeword))]
        poly = Polynomial.interpolate_domain(last_domain, last_codeword)
        #coefficients = intt(last_omega, last_codeword)
        #poly = Polynomial(coefficients).scale(last_offset.inverse())

        # verify by  evaluating
        assert(poly.evaluate_domain(last_domain) ==
               last_codeword), "re-evaluated codeword does not match original!"
        if poly.degree() > degree:
            print("last codeword does not correspond to polynomial of low enough degree")
            print("observed degree:", poly.degree())
            print("but should be:", degree)
            return False

        # get indices
        top_level_indices = self.sample_indices(proof_stream.verifier_fiat_shamir(
        ), self.domain_length >> 1, self.domain_length >> (self.num_rounds()-1), self.num_colinearity_tests)

        # for every round, check consistency of subsequent layers
        for r in range(0, self.num_rounds()-1):

            # fold c indices
            c_indices = [index % (self.domain_length >> (r+1))
                         for index in top_level_indices]

            # infer a and b indices
            a_indices = [index for index in c_indices]
            b_indices = [index + (self.domain_length >> (r+1))
                         for index in a_indices]

            # read values and check colinearity
            aa = []
            bb = []
            cc = []
            for s in range(self.num_colinearity_tests):
                (ay, by, cy) = proof_stream.pull()
                aa += [ay]
                bb += [by]
                cc += [cy]

                # record top-layer values for later verification
                if r == 0:
                    polynomial_values += [(a_indices[s], ay),
                                          (b_indices[s], by)]

                # colinearity check
                ax = offset * (omega ^ a_indices[s])
                bx = offset * (omega ^ b_indices[s])
                cx = alphas[r]
                if test_colinearity([(ax, ay), (bx, by), (cx, cy)]) == False:
                    print("colinearity check failure")
                    return False

            # verify authentication paths
            for i in range(self.num_colinearity_tests):
                salt, path = proof_stream.pull()
                if SaltedMerkle.verify(roots[r], a_indices[i], salt, path, aa[i]) == False:
                    print("merkle authentication path verification fails for aa")
                    return False
                salt, path = proof_stream.pull()
                if SaltedMerkle.verify(roots[r], b_indices[i], salt, path, bb[i]) == False:
                    print("merkle authentication path verification fails for bb")
                    return False
                salt, path = proof_stream.pull()
                if SaltedMerkle.verify(roots[r+1], c_indices[i], salt, path, cc[i]) == False:
                    print("merkle authentication path verification fails for cc")
                    return False

            # square omega and offset to prepare for next round
            omega = omega ^ 2
            offset = offset ^ 2

        # all checks passed
        return True
