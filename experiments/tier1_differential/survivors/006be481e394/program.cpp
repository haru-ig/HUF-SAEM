#include <bits/stdc++.h>
#include <cstring>
#include <iostream>
#include <cstring>
#include <string>

 
int compare_strings(const void* str1, const void* str2) {
    const char* s1 = static_cast<const char*>(str1);
    const char* s2 = static_cast<const char*>(str2);
    if (s1 == nullptr && s2 == nullptr) return 0;
    if (s1 == nullptr) return -1;
    if (s2 == nullptr) return 1;
    return strcmp(s1, s2);
}

int main() {
    std::string string1 = "Hello";
    std::string string2 = "World";
    
    int result = compare_strings(&string1, &string2);
    if (result == 0) {
        std::cout << "Strings are equal." << std::endl;
    } else if (result < 0) {
        std::cout << "First string is less than the second." << std::endl;
    } else {
        std::cout << "First string is greater than the second." << std::endl;
    }
    
    return 0;
}
