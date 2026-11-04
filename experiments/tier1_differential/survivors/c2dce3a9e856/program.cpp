#include <bits/stdc++.h>
#include <cstring>
#include <iostream>
#include <cstring>

int compareStrings(const void* str1, const void* str2) {
    const char* s1 = static_cast<const char*>(str1);
    const char* s2 = static_cast<const char*>(str2);
    if (s1 == nullptr && s2 == nullptr) return 0;
    if (s1 == nullptr) return -1;
    if (s2 == nullptr) return 1;
    return strcmp(s1, s2);
}

int main() {
    const char* str1 = "Hello";
    const char* str2 = "World";
    int result = compareStrings(str1, str2);
    
    if (result == 0) {
        std::cout << "The strings are equal." << std::endl;
    } else if (result < 0 || result > 0) {
        std::cout << "The strings are different. Result: " << result << std::endl;
    } else {  
        std::cout << "One string is null." << std::endl;
    }
    
    return 0;
}
