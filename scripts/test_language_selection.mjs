import assert from 'node:assert/strict';
import test from 'node:test';
import { cppStandard, inferLanguage, latestCppLanguage } from '../frontend/src/composables/languageSelection.js';

const option = (label, value = label) => ({ label, value });

test('C++26 beats C++98 regardless of upstream order or compiler release', () => {
  const languages = [option('C++98 (GCC 15)'), option('C++23 (GCC 14)'), option('C++26 (GCC 14)')];
  assert.equal(latestCppLanguage(languages).value, 'C++26 (GCC 14)');
  for (const name of ['A.cpp', 'A.cc', 'A.cxx', 'A.CPP']) {
    assert.equal(inferLanguage(languages, name).value, 'C++26 (GCC 14)');
  }
});

test('all historical standards sort by year', () => {
  const labels = ['C++98', 'C++03', 'C++11', 'C++14', 'C++17', 'C++20', 'C++23', 'C++26'];
  assert.deepEqual(labels.map((label) => cppStandard(option(label))), [1998, 2003, 2011, 2014, 2017, 2020, 2023, 2026]);
});

test('GNU draft aliases and label-only numeric IDs use actual available values', () => {
  const aliases = ['GNU++0x', 'GNU++1y', 'GNU++1z', 'GNU++2a', 'GNU++2b', 'GNU++2c'];
  assert.deepEqual(aliases.map((label) => cppStandard(option(label))), [2011, 2014, 2017, 2020, 2023, 2026]);
  const languages = [option('GNU G++98', '1'), option('GNU G++2b', '2'), option('GNU++2c', '3')];
  assert.equal(latestCppLanguage(languages).value, '3');
});

test('C++26 absence falls back to the newest advertised standard', () => {
  assert.equal(latestCppLanguage([option('C++98'), option('C++17'), option('C++20')]).value, 'C++20');
  assert.equal(latestCppLanguage([option('C++98')]).value, 'C++98');
  assert.equal(latestCppLanguage([option('Python 3')]), null);
});

test('pasted C++ source is recognized without an extension', () => {
  const languages = [option('C++98'), option('C++26'), option('Python3')];
  for (const code of ['#include <bits/stdc++.h>', '#include <iostream>', 'std::cout << 1;', 'using namespace std;', 'template<typename T> struct S {};']) {
    assert.equal(inferLanguage(languages, '', code).value, 'C++26');
  }
  assert.equal(inferLanguage(languages, '', 'int main() {}'), null);
  assert.equal(inferLanguage(languages, 'main.py', '# std:: is a C++ namespace').value, 'Python3');
});

test('ties preserve advertised order and compiler version is not a standard', () => {
  assert.equal(cppStandard(option('C++ (GCC 26)')), -1);
  assert.equal(latestCppLanguage([option('GNU G++26', 'gcc'), option('Clang C++26', 'clang')]).value, 'gcc');
  assert.equal(cppStandard(option('ISO C++ 2026', '1')), 2026);
});
