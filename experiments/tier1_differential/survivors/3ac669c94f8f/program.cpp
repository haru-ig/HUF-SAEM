#include <bits/stdc++.h>
#include <cstring>
#include <bits/stdc++.h>
#include <cstring>
#include <iostream>

 
int compareStrings(const void* a, const void* b) {
    const char* strA = static_cast<const char*>(a);
    const char* strB = static_cast<const char*>(b);
    if (strA == nullptr && strB == nullptr) return 0;
    if (strA == nullptr) return -1;
    if (strB == nullptr) return 1;
    return std::strcmp(strA, strB);
}

 
int stringCompare(const std::string& str1, const std::string& str2) {
    if (str1.empty() && str2.empty()) return 0;
    if (str1.empty()) return -1;
    if (str2.empty()) return 1;
    if (str1 < str2) return -1;
    if (str1 > str2) return 1;
    return 0;
}

int main() {
    std::string str1 = "Hello";
    std::string str2 = "World";
    
    int result = compareStrings(&str1, &str2);
    if (result == 0) {
        std::cout << "The strings are equal." << std::endl;
    } else if (result < 0) {
        std::cout << "The first string is less than the second string." << std::endl;
    } else {
        std::cout << "The first string is greater than the second string." << std::endl;
    }
    
    int strCompResult = stringCompare(str1, str2);
    if (strCompResult == 0) {
        std::cout << "The strings are equal according to std::string compare." << std::endl;
    } else if (strCompResult < 0) {
        std::cout << "The first string is less than the second string according to std::string compare." << std::endl;
    } else {
        std::cout << "The first string is greater than the second string according to std::string compare." << std::endl;
    }
    
    return 0;
}
